from typing import Optional
import asyncio
import io
from pathlib import Path
import re
from copy import copy

import aiofiles
import asyncssh


from . logger import get_logger
from . import consts


__all__ = [
    'ConfigBackupSSHSpec',
    'set_max_startups'
]


class ConfigBackupSSHSpec(object):
    """
    The ConfigBackupSSHSpec class is used to define and process the
    configuration file backup process over SSH.  The primary usage is to
    initialize the class with the host configuration, the operating system
    specification, and the application configuration.  Once initialized the
    Caller should await on `backup_config()` to execute the backup process.

    If the ConfigBackupSSHSpec defines `disable_paging` will execute those
    commands before executinig the `show_running` command.

    If the ConfigBackupSSHSpec does not define the `disable_paging` attribute,
    then only the `show_running` command will be executed.


    Attributes
    ----------
    cls.PROMPT_PATTERN: re.Pattern
        compiled regular expression this used to find the CLI prompt

    cls.show_running: str
        The device CLI command that when executed will produce the output of
        the running configuraiton

    cls.disable_paging: Optional[Union[str,list]]
        The device CLI command(s) that when execute will disable paging so that
        when the show-running command is executed the output will not be
        blocked with a "--More--" user prompt.
    """
    PROMPT_PATTERN = re.compile(r"^\r?([a-z0-9.\-_@()/:]{1,32}\s*[#>$])\s*$", flags=(re.M | re.I))

    show_running = 'show running-config'
    disable_paging = None

    _max_startups_sem4 = asyncio.Semaphore(consts.DEFAULT_MAX_STARTUPS)

    def __init__(
        self,
        host_cfg: dict,
        os_spec: dict,
        app_cfg: dict
    ):
        """
        Initialize the backup spec with information about the host, the os_spec assigned
        to the host, and the command app configuration.

        Parameters
        ----------
        host_cfg
        os_spec
        app_cfg
        """
        self.host_cfg = host_cfg
        self.name = host_cfg.get('host') or host_cfg.get('ipaddr')
        self.app_cfg = app_cfg
        self.os_spec = copy(os_spec)
        self.log = get_logger()

        if self.disable_paging and 'disable_paging' not in os_spec:
            self.os_spec['disable_paging'] = self.disable_paging

        self.os_name = host_cfg['os_name']

        self.conn_args = {
            'host': self.host_cfg.get('ipaddr') or self.host_cfg.get('host'),
            'known_hosts': None
        }

        self._cur_prompt: Optional[str] = None
        self.config = None
        self.save_file = None
        self.failed = None

        self.conn = None
        self.process: Optional[asyncssh.SSHClientProcess] = None

    @classmethod
    def set_max_startups(cls, max_startups):
        cls._max_startups_sem4 = asyncio.Semaphore(value=max_startups)

    # -------------------------------------------------------------------------
    #
    #                       Backup Config Coroutine Task
    #
    # -------------------------------------------------------------------------

    async def backup_config(self):
        """
        This is the "main" coroutine that should be used as Task that will
        perform each of the steps to backup the running configuration to a text
        file.

        Returns
        -------
        ConfigBackupSSHSpec
            Instance of self so that information about the process can be
            examined once the backup process completes, or fails.
        """

        async with await self.login():
            try:
                await self.get_running_config()

            except Exception as exc:
                self.log.error(f"BACKUP FAILED: {str(exc)}")

            finally:
                await self.close()

        if self.config:
            await self.save_config()

        return self

    # -------------------------------------------------------------------------
    #
    #                       Test Login Coroutine Task
    #
    # -------------------------------------------------------------------------

    async def test_login(self) -> Optional[str]:
        login_as = None

        try:
            async with await self.login():
                login_as = self.conn_args['username']

        except asyncssh.PermissionDenied:
            pass

        return login_as

    # -------------------------------------------------------------------------
    #
    #                            Get Configuration
    #
    # -------------------------------------------------------------------------

    async def get_running_config(self):
        command = self.show_running

        if not self.process:
            self.log.info(f"GET-CONFIG: {self.name}")
            res = await self.conn.run(command)
            self.conn.close()
            ln_at = res.stdout.find(command) + len(command) + 1
            self.config = res.stdout[ln_at:]
            return

        at_prompt = False
        paging_disabled = False

        try:
            await asyncio.wait_for(self.read_until_prompt(), timeout=30)
            at_prompt = True

            await asyncio.wait_for(self.run_disable_paging(), timeout=30)
            paging_disabled = True

            self.log.info(f"GET-CONFIG: {self.name}")
            self.config = await asyncio.wait_for(self.run_command(command), timeout=120)

        except asyncio.TimeoutError:
            raise RuntimeError(
                f'Timeout when getting running configuraiton',
                dict(at_prompt=at_prompt, paging_disabled=paging_disabled)
            )

    # -------------------------------------------------------------------------
    #
    #                            Login / Close
    #
    # -------------------------------------------------------------------------

    async def login(self):
        """
        This coroutine is used to execute the SSH login process to the target device.
        Each of the `credentials` provided in the app-configure are tried in the order
        they were provided in the configuration file.  If the host configuraiton included
        credentials, these will be used first.

        When this coroutine completes successfully the `conn` attribute is
        initialized to the SSHClientConnection.  If this SSHSpec requires the
        use of multiple commands, so that paging can be disabled, then the
        `process` attribute is also initiazed so that the `process.stdin` and
        `process.stdout` can be used to interact with the SSH CLI.

        Returns
        -------
        The AsyncSSH will obtain a protocol connection instance from asyncio.loop.create_connection(), and
        then enrobe it with an async context manager so that the Caller can either use the instnace
        directly or as a context manager.

        Raises
        ------
        asyncssh.PermissionDenied
            When none of the provided credentials result in a successful login.

        asyncio.TimeoutError
            When attempting to connect to a device exceeds the timeout value.
        """

        creds = copy(self.app_cfg['credentials'])

        # if there are host specific credentials, then try these first.

        host_creds = dict(username=self.host_cfg.get('username'),
                          password=self.host_cfg.get('password'))

        if all(host_creds.values()):
            creds.insert(0, host_creds)

        # TODO:
        #       if there are os_spec specific credentials then add those next

        # interate through all of the credential options until one is accepted.
        # the number of max setup connections is controlled by a semaphore
        # instance so that the server running this code is not overwhelmed.

        for try_cred in creds:
            try:
                self.failed = None
                self.conn_args.update(try_cred)
                async with self.__class__._max_startups_sem4:

                    login_msg = (f"LOGIN: {self.name} ({self.os_name}) "
                                 f"as {self.conn_args['username']}")

                    self.log.info(login_msg)
                    self.conn = await asyncio.wait_for(asyncssh.connect(**self.conn_args), timeout=60)
                    self.log.info(f"CONNECTED: {self.name}")

                    if (
                        'disable_paging' in self.os_spec or
                        self.disable_paging
                    ):
                        self.process = await self.conn.create_process(term_type='vt100')

                    return self.conn

            except asyncssh.PermissionDenied as exc:
                self.failed = exc
                continue

        # Indicate that the login failed with the number of credential
        # attempts.

        raise asyncssh.PermissionDenied(
            reason=f'No valid username/password ({len(creds)})'
        )

    async def close(self):
        self.conn.close()
        await self.conn.wait_closed()
        self.log.info(f"CLOSED: {self.name}")

    # -------------------------------------------------------------------------
    #
    #                                    Helpers
    #
    # -------------------------------------------------------------------------

    async def read_until_prompt(self):
        output = ''
        while True:
            output += await self.process.stdout.read(io.DEFAULT_BUFFER_SIZE)
            nl_at = output.rfind('\n')
            if mobj := self.PROMPT_PATTERN.match(output[nl_at + 1:]):
                self._cur_prompt = mobj.group(1)
                return output[0:nl_at]

    async def run_command(self, command):
        wr_cmd = command + "\n"
        self.process.stdin.write(wr_cmd)
        output = await self.read_until_prompt()
        return output[len(wr_cmd) + 1:]

    async def run_disable_paging(self):
        """
        This coroutine is used to execute each of the `disable_paging` commands
        so that the CLI will not prompt for "--More--" output.
        """

        disable_paging_commands = self.os_spec['disable_paging']
        if not isinstance(disable_paging_commands, list):
            disable_paging_commands = [disable_paging_commands]

        for cmd in disable_paging_commands:
            # TODO: need to check result for errors
            await self.run_command(cmd)

    # -------------------------------------------------------------------------
    #
    #                         Store Config to Filesystem
    #
    # -------------------------------------------------------------------------

    async def save_config(self):
        self.save_file = Path(self.app_cfg['defaults']['configs_dir']) / f"{self.name}.cfg"
        async with aiofiles.open(self.save_file, mode='w+') as ofile:
            await ofile.write(self.config.replace("\r", ""))
            await ofile.write("\n")


def set_max_startups(count, cls=ConfigBackupSSHSpec):
    cls.set_max_startups(count)
