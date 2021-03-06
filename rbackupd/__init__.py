# -*- encoding: utf-8 -*-
# Copyright (c) 2013 Hannes Körber <hannes.koerber+rbackupd@gmail.com>
#
# This file is part of rbackupd.
#
# rbackupd is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# rbackupd is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import collections
import datetime
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import time

from . import config
from . import constants as const
from . import cron
from . import files
from . import filesystem
from . import interval
from . import levelhandler
from . import repository
from . import rsync


def set_up_logging(console_loglevel, logfile_loglevel):
    global logger
    global logging_console_handlers
    global logging_memory_handler
    global logging_file_handlers
    # setting logleve to minimum level, as the handlers take care of the
    # filtering by level
    logger.setLevel(logging.DEBUG)

    # console handlers
    stdout_handler = logging.StreamHandler(sys.stdout)
    stderr_handler = logging.StreamHandler(sys.stderr)

    stdout_handler.addFilter(levelhandler.LevelFilter(
        minlvl=logging.NOTSET,
        maxlvl=logging.WARNING - 1))
    stderr_handler.addFilter(levelhandler.LevelFilter(
        minlvl=logging.WARNING,
        maxlvl=logging.CRITICAL))

    stdout_handler.setLevel(console_loglevel)
    stderr_handler.setLevel(console_loglevel)

    console_formatter = logging.Formatter(
        fmt="[{asctime}] {message}",
        datefmt="%H:%M:%S",
        style='{')

    stdout_handler.setFormatter(console_formatter)
    stderr_handler.setFormatter(console_formatter)

    logger.addHandler(stdout_handler)
    logger.addHandler(stderr_handler)

    logging_console_handlers.append(stdout_handler)
    logging_console_handlers.append(stderr_handler)

    # logfile_handlers
    logging_memory_handler = logging.handlers.MemoryHandler(
        capacity=1000000,
        flushLevel=logging.CRITICAL + 1,   # we do not want it to auto-flush
        target=None)   # we will set the target when a logfile is available

    logging_memory_handler.setLevel(logfile_loglevel)

    global logfile_formatter
    logfile_formatter = logging.Formatter(
        fmt="[{asctime},{msecs}] [{levelname}] {filename}: {message}",
        datefmt="%Y-%m-%d %H:%M:%S",
        style='{')

    logging_memory_handler.setFormatter(logfile_formatter)

    logger.addHandler(logging_memory_handler)

    logging_file_handlers.append(logging_memory_handler)


def change_console_logging_level(loglevel):
    for handler in logging_console_handlers:
        handler.setLevel(loglevel)


def change_file_logging_level(loglevel):
    for handler in logging_file_handlers:
        handler.setLevel(loglevel)


def change_to_logfile_logging(logfile_path, loglevel):
    global logging_memory_handler
    if logging_memory_handler is None:
        pass

    logfile_handler = logging.handlers.RotatingFileHandler(
        logfile_path,
        mode='a',
        maxBytes=1000000,
        backupCount=9)

    logfile_handler.setLevel(loglevel)

    logfile_handler.setFormatter(logfile_formatter)

    logging_memory_handler.setTarget(logfile_handler)
    logging_memory_handler.flush()
    logging_memory_handler.close()

    logger.addHandler(logfile_handler)
    logging_file_handlers.append(logfile_handler)

    logger.removeHandler(logging_memory_handler)
    logging_file_handlers.remove(logging_memory_handler)
    logging_memory_handler = None


def main(config_file, console_loglevel):
    try:
        change_console_logging_level(console_loglevel)
        run(config_file)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt.")
        sys.exit(const.EXIT_KEYBOARD_INTERRUPT)
    except SystemExit as err:
        logger.info("Exiting with code %s.", err.code)
        sys.exit(err.code)


def run(config_file):
    if not os.path.isfile(config_file):
        if not os.path.exists(config_file):
            logger.critical("Config file not found. Aborting.")
            sys.exit(const.EXIT_CONFIG_FILE_NOT_FOUND)
        else:
            logger.critical("Invalid config file. Aborting.")
            sys.exit(const.EXIT_INVALID_CONFIG_FILE)

    try:
        conf = config.Config(config_file)
    except config.ParseError as err:
        logger.critical("Invalid config file:\nerror line %s (\"s\"): %s",
                        err.lineno,
                        err.line,
                        err.message)

    # this is the [logging] section
    conf_section_logging = conf.get_section(const.CONF_SECTION_LOGGING)
    conf_logfile_path = conf_section_logging[const.CONF_KEY_LOGFILE_PATH][0]
    conf_loglevel = conf_section_logging[const.CONF_KEY_LOGLEVEL][0]

    if conf_loglevel not in const.CONF_VALUES_LOGLEVEL:
        logger.critical("Invalid value for key \"%s\": \"%s\". Valid values: "
                        "%s. Aborting.",
                        const.CONF_KEY_LOGLEVEL,
                        conf_loglevel,
                        ",".join(const.CONF_VALUES_LOGLEVEL))
        sys.exit(const.EXIT_INVALID_CONFIG_FILE)
    if conf_loglevel == "quiet":
        conf_loglevel = logging.WARNING
    elif conf_loglevel == "default":
        conf_loglevel = logging.INFO
    elif conf_loglevel == "verbose":
        conf_loglevel = logging.VERBOSE
    elif conf_loglevel == "debug":
        conf_loglevel = logging.DEBUG
    else:
        assert(False)

    logfile_dir = os.path.dirname(conf_logfile_path)
    if not os.path.exists(logfile_dir):
        os.mkdir(logfile_dir)

    # now we can change from logging into memory to logging to the logfile
    change_to_logfile_logging(logfile_path=conf_logfile_path,
                              loglevel=conf_loglevel)

    # this is the [rsync] section
    conf_section_rsync = conf.get_section(const.CONF_SECTION_RSYNC)
    conf_rsync_cmd = conf_section_rsync.get(const.CONF_KEY_RSYNC_CMD, "rsync")

    conf_sections_mounts = conf.get_sections(const.CONF_SECTION_MOUNT)

    # these are the [default] options that can be overwritten in the specific
    # [task] section
    conf_section_default = conf.get_section(const.CONF_SECTION_DEFAULT)
    conf_default_rsync_logfile = conf_section_default.get(
        const.CONF_KEY_RSYNC_LOGFILE, None)
    conf_default_rsync_logfile_name = conf_section_default.get(
        const.CONF_KEY_RSYNC_LOGFILE_NAME, None)
    conf_default_rsync_logfile_format = conf_section_default.get(
        const.CONF_KEY_RSYNC_LOGFILE_FORMAT, None)

    conf_default_filter_patterns = conf_section_default.get(
        const.CONF_KEY_FILTER_PATTERNS, None)

    conf_default_include_patterns = conf_section_default.get(
        const.CONF_KEY_INCLUDE_PATTERNS, None)
    conf_default_exclude_patterns = conf_section_default.get(
        const.CONF_KEY_EXCLUDE_PATTERNS, None)

    conf_default_include_files = conf_section_default.get(
        const.CONF_KEY_INCLUDE_FILE, None)
    conf_default_exclude_files = conf_section_default.get(
        const.CONF_KEY_EXCLUDE_FILE, None)

    conf_default_create_destination = conf_section_default.get(
        const.CONF_KEY_CREATE_DESTINATION, None)

    conf_default_one_filesystem = conf_section_default.get(
        const.CONF_KEY_ONE_FILESYSTEM, None)

    conf_default_rsync_args = conf_section_default.get(
        const.CONF_KEY_RSYNC_ARGS, None)

    conf_default_ssh_args = conf_section_default.get(
        const.CONF_KEY_SSH_ARGS, None)

    conf_default_overlapping = conf_section_default.get(
        const.CONF_KEY_OVERLAPPING, None)

    conf_sections_tasks = conf.get_sections(const.CONF_SECTION_TASK)

    if conf_sections_mounts is None:
        conf_sections_mounts = []

    for mount in conf_sections_mounts:
        if len(mount) == 0:
            continue

        conf_partition = mount[const.CONF_KEY_PARTITION][0]
        conf_mountpoint = mount[const.CONF_KEY_MOUNTPOINT][0]
        conf_mountpoint_ro = mount.get(const.CONF_KEY_MOUNTPOINT_RO, [None])[0]
        conf_mountpoint_options = mount.get(
            const.CONF_KEY_MOUNTPOINT_OPTIONS, [None])[0]
        conf_mountpoint_ro_options = mount.get(
            const.CONF_KEY_MOUNTPOINT_RO_OPTIONS, [None])[0]

        if conf_mountpoint_options is None:
            conf_mountpoint_options = ['rw']
        else:
            conf_mountpoint_options = conf_mountpoint_options.split(',')
            conf_mountpoint_options.append('rw')

        if conf_mountpoint_ro_options is None:
            conf_mountpoint_ro_options = ['ro']
        else:
            conf_mountpoint_ro_options = conf_mountpoint_ro_options.split(',')
            conf_mountpoint_ro_options.append('ro')

        conf_mountpoint_create = mount[const.CONF_KEY_MOUNTPOINT_CREATE][0]

        conf_mountpoint_ro_create = mount.get(
            const.CONF_KEY_MOUNTPOINT_RO_CREATE, [None])[0]
        if (conf_mountpoint_ro is not None and
                conf_mountpoint_ro_create is None):
            logger.critical("Key \"mountpoint_ro_create\" needed if key "
                            "\"mountpoint_ro\" is present. Aborting.")
            sys.exit(const.EXIT_NO_MOUNTPOINT_CREATE)

        if (conf_mountpoint_ro is not None and conf_mountpoint_ro_create and
                not os.path.exists(conf_mountpoint_ro)):
            os.mkdir(conf_mountpoint_ro)
        if not os.path.exists(conf_mountpoint_ro):
            logger.critical("Path of \"mountpoint_ro\" does not exist. "
                            "Aborting.")
            sys.exit()
        if conf_mountpoint_create and not os.path.exists(conf_mountpoint):
            os.mkdir(conf_mountpoint)
        if not os.path.exists(conf_mountpoint):
            logger.critical("Path of \"mountpoint\" does not exist. Aborting")
            sys.exit()

        if conf_partition.startswith('UUID'):
            uuid = conf_partition.split('=')[1]
            partition_identifier = filesystem.PartitionIdentifier(uuid=uuid)
        elif conf_partition.startswith('LABEL'):
            label = conf_partition.split('=')[1]
            partition_identifier = filesystem.PartitionIdentifier(label=label)
        else:
            partition_identifier = filesystem.PartitionIdentifier(
                path=conf_device)

        partition = filesystem.Partition(partition_identifier,
                                         filesystem="auto")

        mountpoint = filesystem.Mountpoint(
            path=conf_mountpoint,
            options=conf_mountpoint_options)
        # How to get two mounts of the same device with different rw/ro:
        # mount readonly
        # bind readonly to the writeable mountpoint without altering rw/ro
        # remount writeable mountpoint with rw
        if conf_mountpoint_ro is not None:
            mountpoint_ro = filesystem.Mountpoint(
                path=conf_mountpoint_ro,
                options=conf_mountpoint_ro_options)
            try:
                partition.mount(mountpoint_ro)
            except filesystem.MountpointInUseError as err:
                logger.warning("Mountpoint \"%s\" already in use. "
                               "Skipping mounting." % err.path)

            try:
                mountpoint_ro.bind(mountpoint)
            except filesystem.MountpointInUseError as err:
                logger.warning("Mountpoint \"%s\" already in use. "
                               "Skipping mounting." % err.path)
            mountpoint.remount(("rw", "relatime", "noexec", "nosuid"))
        else:
            try:
                partition.mount(mountpoint)
            except filesystem.MountpointInUseError as err:
                logger.warning("Mountpoint \"%s\" already in use. "
                               "Skipping mounting.", err.path)

    repositories = []
    for task in conf_sections_tasks:
        # these are the options given in the specific tasks. if none are given,
        # the default values from the [default] sections will be used.
        conf_rsync_logfile = task.get(
            const.CONF_KEY_RSYNC_LOGFILE, conf_default_rsync_logfile)
        conf_rsync_logfile_name = task.get(const.CONF_KEY_RSYNC_LOGFILE_NAME,
                                           conf_default_rsync_logfile_name)[0]
        conf_rsync_logfile_format = task.get(
            const.CONF_KEY_RSYNC_LOGFILE_FORMAT,
            conf_default_rsync_logfile_format)[0]

        conf_filter_patterns = task.get(
            const.CONF_KEY_FILTER_PATTERNS, conf_default_filter_patterns)

        conf_include_patterns = task.get(
            const.CONF_KEY_INCLUDE_PATTERNS, conf_default_include_patterns)
        conf_exclude_patterns = task.get(
            const.CONF_KEY_EXCLUDE_PATTERNS, conf_default_exclude_patterns)

        conf_include_files = task.get(
            const.CONF_KEY_INCLUDE_FILE, conf_default_include_files)
        conf_exclude_files = task.get(
            const.CONF_KEY_EXCLUDE_FILE, conf_default_exclude_files)

        conf_create_destination = task.get(
            const.CONF_KEY_CREATE_DESTINATION, conf_default_create_destination)

        conf_one_filesystem = task.get(
            const.CONF_KEY_ONE_FILESYSTEM, conf_default_one_filesystem)[0]

        conf_rsync_args = task.get(
            const.CONF_KEY_RSYNC_ARGS, conf_default_rsync_args)

        conf_ssh_args = task.get(
            const.CONF_KEY_SSH_ARGS, conf_default_ssh_args)
        conf_ssh_args = const.SSH_CMD + " " + " ".join(conf_ssh_args)

        conf_overlapping = task.get(
            const.CONF_KEY_OVERLAPPING, conf_default_overlapping)[0]

        # these are the options that are not given in the [default] section.
        conf_destination = task[const.CONF_KEY_DESTINATION][0]
        conf_sources = task[const.CONF_KEY_SOURCE]

        if conf_overlapping not in ["single", "hardlink", "symlink"]:
            logger.critical("Invalid value for key \"overlapping\": %s"
                            "Valid values: \"single\", \"hardlink\", "
                            "\"symlink\". Aborting.", conf_overlapping)
            sys.exit(EXIT_INVALID_CONFIG_FILE)

        # now we can check the values
        if not os.path.exists(conf_destination):
            if not conf_create_destination:
                logger.error("Destination \"%s\" does not exists, will no be "
                             "created. Repository will be skipped.",
                             conf_destination)
                continue
        if not os.path.isdir(conf_destination):
            logger.critical("Destination \"%s\" not a directory. Aborting.",
                            conf_destination)
            sys.exit(const.EXIT_INVALID_DESTINATION)

        if conf_include_files is not None:
            for include_file in conf_include_files:
                if include_file is None:
                    continue
                if not os.path.exists(include_file):
                    logger.critical("Include file \"%s\" not found. "
                                    "Aborting.", include_file)
                    sys.exit(const.EXIT_INCLUDE_FILE_NOT_FOUND)
                elif not os.path.isfile(include_file):
                    logger.critical("Include file \"%s\" is not a file. "
                                    "Aborting.", include_file)
                    sys.exit(const.EXIT_INCLUDE_FILE_INVALID)

        if conf_exclude_files is not None:
            for exclude_file in conf_exclude_files:
                if exclude_file is None:
                    continue
                if not os.path.exists(exclude_file):
                    logger.critical("Exclude file \"%s\" not found. "
                                    "Aborting.", exclude_file)
                    sys.exit(const.EXIT_EXCULDE_FILE_NOT_FOUND)
                elif not os.path.isfile(exclude_file):
                    logger.critical("Exclude file \"%s\" is not a file. "
                                    "Aborting.", exclude_file)
                    sys.exit(const.EXIT_EXCLUDE_FILE_INVALID)

        if conf_rsync_logfile:
            conf_rsync_logfile_options = rsync.LogfileOptions(
                conf_rsync_logfile_name, conf_rsync_logfile_format)
        else:
            conf_rsync_logfile_options = None

        conf_rsyncfilter = rsync.Filter(conf_include_patterns,
                                        conf_exclude_patterns,
                                        conf_include_files,
                                        conf_exclude_files,
                                        conf_filter_patterns)

        rsync_args = []
        for arg in conf_rsync_args:
            rsync_args.extend(arg.split())
        conf_rsync_args = rsync_args

        if conf_one_filesystem:
            conf_rsync_args.append("-x")

        ssh_args = []
        if conf_ssh_args is not None:
            ssh_args = ["--rsh", conf_ssh_args]
        conf_rsync_args.extend(ssh_args)

        conf_taskname = task[const.CONF_KEY_TASKNAME][0]
        conf_task_intervals = task[const.CONF_KEY_INTERVAL]
        conf_task_keeps = task[const.CONF_KEY_KEEP]
        conf_task_keep_age = task[const.CONF_KEY_KEEP_AGE]

        task_keep_age = collections.OrderedDict()
        for (backup_interval, max_age) in conf_task_keep_age.items():
            task_keep_age[backup_interval] = \
                interval.interval_to_oldest_datetime(max_age)

        repositories.append(
            repository.Repository(conf_sources,
                                  conf_destination,
                                  conf_taskname,
                                  conf_task_intervals,
                                  conf_task_keeps,
                                  task_keep_age,
                                  conf_rsyncfilter,
                                  conf_rsync_logfile_options,
                                  conf_rsync_args))

    while True:
        start = datetime.datetime.now()
        for repo in repositories:

            for (backup_interval, max_age) in conf_task_keep_age.items():
                task_keep_age[backup_interval] = \
                    interval.interval_to_oldest_datetime(max_age)
            repo.keep_age = task_keep_age

            create_backups_if_necessary(repo, conf_overlapping,
                                        conf_rsync_cmd)
            handle_expired_backups(repo, start)

        # we have to get the current time again, as the above might take a lot
        # of time
        now = datetime.datetime.now()
        if now.minute == 59:
            wait_seconds = 60 - now.second
        else:
            nextmin = now.replace(minute=now.minute+1, second=0, microsecond=0)
            wait_seconds = (nextmin - now).seconds + 1
        time.sleep(wait_seconds)


def create_backups_if_necessary(repository, conf_overlapping, conf_rsync_cmd):
    necessary_backups = repository.get_necessary_backups()
    if len(necessary_backups) != 0:
        if conf_overlapping == "single":
            new_backup_interval_name = None
            exitloop = False
            for (name, _) in repository.intervals:
                for (backup_name, _) in necessary_backups:
                    if name == backup_name:
                        new_backup_interval_name = name
                        exitloop = True
                        break
                if exitloop:
                    break
            new_backup = repository.get_backup_params(new_backup_interval_name)
            create_backup(new_backup, conf_rsync_cmd)

        else:
            # Make one "real" backup and just hard/symlink all others to this
            # one
            timestamp = datetime.datetime.now()
            real_backup = repository.get_backup_params(necessary_backups[0][0],
                                                       timestamp=timestamp)
            create_backup(real_backup, conf_rsync_cmd)
            for backup in necessary_backups[1:]:
                backup = repository.get_backup_params(backup[0], timestamp)
                # real_backup.destination and backup.destination are guaranteed
                # to be identical as they are from the same repository
                source = os.path.join(real_backup.destination,
                                      real_backup.folder)
                destination = os.path.join(real_backup.destination,
                                           backup.folder)
                if conf_overlapping == "hardlink":
                    logger.info("Hardlinking snapshot \"%s\" into \"%s\"",
                                os.path.basename(source),
                                os.path.basename(destination))
                    files.copy_hardlinks(source, destination)
                elif conf_overlapping == "symlink":
                    # We should create RELATIVE symlinks with "-r", as the
                    # repository might move, but the relative location of all
                    # backups will stay the same
                    logger.info("Symlinking \"%s\" to \"%s\"",
                                os.path.basename(source),
                                os.path.basename(destination))
                    files.create_symlink(source, destination)
    else:
        logger.info("No backup necessary.")


def create_backup(new_backup, rsync_cmd):
    destination = os.path.join(new_backup.destination,
                               new_backup.folder)
    symlink_latest = os.path.join(new_backup.destination,
                                  const.SYMLINK_LATEST_NAME)
    for source in new_backup.sources:
        if new_backup.link_ref is None:
            link_dest = None
        else:
            link_dest = os.path.join(new_backup.destination,
                                     new_backup.link_ref)
        logger.info("Creating backup \"%s\".", os.path.basename(destination))
        (returncode, stdoutdata, stderrdata) = rsync.rsync(
            rsync_cmd,
            source,
            destination,
            link_dest,
            new_backup.rsync_args,
            new_backup.rsyncfilter,
            new_backup.rsync_logfile_options)
        if returncode != 0:
            logger.critical("Rsync failed. Aborting. Stderr:\n%s", stderrdata)
            sys.exit(const.EXIT_RSYNC_FAILED)
        else:
            logger.info("Backup finished successfully.")
    if os.path.islink(symlink_latest):
        files.remove_symlink(symlink_latest)
    files.create_symlink(destination, symlink_latest)


def handle_expired_backups(repository, current_time):
    expired_backups = repository.get_expired_backups()
    if len(expired_backups) > 0:
        for expired_backup in expired_backups:
            # as a backup might be a symlink to another backup, we have to
            # consider: when it is a symlink, just remove the symlink. if not,
            # other backupSSS!! might be a symlink to it, so we have to check
            # all other backups. we overwrite one symlink with the backup and
            # update all remaining symlinks
            logger.info("Expired backup: \"%s\".",
                        expired_backup.name)
            expired_path = os.path.join(repository.destination,
                                        expired_backup.name)
            if os.path.islink(expired_path):
                logger.info("Removing symlink \"%s\".",
                            os.path.basename(expired_path))
                files.remove_symlink(expired_path)
            else:
                symlinks = []
                for backup in repository.backups:
                    backup_path = os.path.join(repository.destination,
                                               backup.name)
                    if (os.path.samefile(expired_path,
                                         os.path.realpath(backup_path))
                            and os.path.islink(backup_path)):
                        symlinks.append(backup)

                if len(symlinks) == 0:
                    # just remove the backups, no symlinks present
                    logger.info("Removing directory \"%s\".",
                                expired_backup.name)
                    files.remove_recursive(os.path.join(
                        repository.destination, expired_backup.name))
                else:
                    # replace the first symlink with the backup
                    symlink_path = os.path.join(repository.destination,
                                                symlinks[0].name)
                    logger.info("Removing symlink \"%s\".",
                                os.path.basename(symlink_path))
                    files.remove_symlink(symlink_path)

                    # move the real backup over

                    logger.info("Moving \"%s\" to \"%s\".",
                                os.path.basename(expired_path),
                                os.path.basename(symlink_path))
                    files.move(expired_path, symlink_path)

                    # now update all symlinks to the directory
                    for remaining_symlink in symlinks[1:]:
                        remaining_symlink_path = os.path.join(
                            repository.destination,
                            remaining_symlink.name)
                        logger.info("Removing symlink \"%s\".",
                                    os.path.basename(remaining_symlink_path))
                        files.remove_symlink(remaining_symlink_path)
                        logger.info("Creating symlink \"%s\" pointing to "
                                    "\"%s\".",
                                    os.path.basename(remaining_symlink_path),
                                    os.path.basename(symlink_path))
                        files.create_symlink(symlink_path,
                                             remaining_symlink_path)
    else:
        logger.info("No expired backups.")


logger = logging.getLogger(__name__)
logging_memory_handler = None
logging_console_handlers = []
logging_file_handlers = []

# custom log levels
logging.VERBOSE = 15
# necessary to get the name in log output instead of an integer
logging.addLevelName(logging.VERBOSE, "VERBOSE")
logging.Logger.verbose = \
    lambda obj, msg, *args, **kwargs: \
    obj.log(logging.VERBOSE, msg, *args, **kwargs)

set_up_logging(console_loglevel=logging.INFO,
               logfile_loglevel=logging.VERBOSE)
