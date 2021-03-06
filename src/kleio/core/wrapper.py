# -*- coding: utf-8 -*-
"""
:mod:`kleio.core.worker.consumer` -- Evaluate objective on a set of parameters
==============================================================================

.. module:: consumer
   :platform: Unix
   :synopsis: Call user's script as a black box process to evaluate a trial.

"""
import asyncio
import concurrent
import logging
import os
import pprint
import subprocess
import sys
import tempfile
import sys

from kleio.core.io.database import Database
from kleio.core.trial.base import Trial


log = logging.getLogger(__name__)



BROKEN = """
You can check log with the following command:
$ kleio cat --stderr {trial.short_id}

To continue execution you can mark the trial as executable with:
$ kleio switchover {trial.short_id}

Or force execution with --switchover option:
$ kleio exec --switchover {trial.short_id}
"""


RESERVE_BROKEN = """

You can mark a broken trial as executable with the following command:
$ kleio switchover {trial.short_id}

You can also use the option --switch-over to force execution.
"""


INTERRUPT = """
***
Execution of '{trial.short_id}' interrupted by user

Execution can be resumed using the same command
$ kleio {trial.commandline}

$ kleio exec {trial.id}
"""


class Consumer(object):
    """Consume a trial by using it to initialize a black-box box to evaluate it.

    It uses an `Experiment` object to push an evaluated trial, if results are
    delivered to the worker process successfully.

    It forks another process which executes user's script with the suggested
    options. It expects results to be written in a **JSON** file, whose path
    has been defined in a special kleio environmental variable which is set
    into the child process' environment.

    """

    def __init__(self, working_dir, capture=False):
        """Initialize a consumer.

        """
        log.debug("Creating Consumer object.")
        self.root_working_dir = os.path.join(working_dir, 'kleio')
        self.capture = capture

    def consume(self, trial):
        """Execute user's script as a block box using the options contained within `trial`.

        Parameters
        ----------
        trial: `kleio.core.worker.trial.Trial`
            Trial container, provides convenient interface for interacting with the database.

        """
        try:
            trial.reserve()
            trial.save()  # update the report
        except RuntimeError as e:
            logging.error("Failed to reserve '{}'".format(trial.short_id))
            logging.error(str(e))

            if trial.status == 'broken':
                # TODO: Move to CLI
                print(RESERVE_BROKEN.format(trial=trial))

            sys.exit(0)

        logging.info("Trial reserved with id: {}".format(trial.short_id))

        # Get path to user's script and infer trial configuration directory

        working_dir = os.path.join(self.root_working_dir, trial.short_id)
        if not os.path.isdir(working_dir):
            log.debug("### Create new directory at '%s':", working_dir)
            os.makedirs(working_dir)

        log.debug("## Working in directory '%s':", working_dir)
        completed_trial = self._consume(trial, working_dir)

        if completed_trial is not None:
            logging.info("Trial successfully executed")
            completed_trial.complete()
            completed_trial.save()
        else:
            logging.error("Trial crashed. Save as broken.")
            # TODO: Move to CLI module
            print(BROKEN.format(trial=trial))
            trial.broken()
            trial.save()

    def _consume(self, trial, working_dir):
        logging.info("Executing command:\n{}".format(trial.commandline))
        trial.running()
        returncode = self.launch_process(trial, working_dir)

        if returncode != 0:
            log.error("Something went wrong. Process "
                      "returned with code %d !", returncode)
            return None

        return trial

    def launch_process(self, trial, working_dir):
        """Facilitate launching a black-box trial."""
        env = dict(os.environ)
        env['KLEIO_TRIAL_ID'] = trial.id
        database = Database()
        env['KLEIO_DB_NAME'] = database.name
        env['KLEIO_DB_TYPE'] = database.__class__.__name__.lower()
        env['KLEIO_DB_ADDRESS'] = database.uri  # TODO: Make it generic, this is only for mongodb
        level_to_verbose = {'WARNING': 0,
                            'INFO': 1,
                            'DEBUG': 2}
        env['KLEIO_VERBOSITY'] = str(
            level_to_verbose.get(
                logging.getLevelName(logging.getLogger().getEffectiveLevel()), 2))

        log.debug("Executing with env:\n{}".format(pprint.pformat(env)))

        # Create the subprocess, redirect the standard output into a pipe
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            task = execute(trial, capture=self.capture, cwd=working_dir, env=env)
            returncode = loop.run_until_complete(task)
        except KeyboardInterrupt as e:
            # TODO: Move to CLI
            print(INTERRUPT.format(trial=trial))
            trial.suspend()
            trial.save()
            raise e
        except BaseException as e:
            trial.broken()
            trial.save()
            raise e
        finally:
            loop.close()

        return returncode

 
@asyncio.coroutine
def update(trial, sleep_time=10):
    while True:
        try:
            yield from asyncio.sleep(sleep_time)
            trial.heartbeat()
            trial.save()
        except concurrent.futures.CancelledError:
            trial.save()
            raise


@asyncio.coroutine
def log_stream(stdlist, stream, capture):
    while not stream.at_eof():
        data = yield from stream.readline()
        if data:
            line = data.decode('ascii').rstrip()
            stdlist.append(line)
            if not capture:
                print(line)


@asyncio.coroutine 
def execute(trial, cwd, env, capture=False, sleep_time=10):
    """
    A task to do for a number of seconds
    """
    # To make sure we have no discrepency between std{out,err} and logged data from clients process
    env['PYTHONUNBUFFERED'] = '1'
    create = asyncio.create_subprocess_exec(*trial.commandline.split(" "),
                                            stdout=asyncio.subprocess.PIPE,
                                            stderr=asyncio.subprocess.PIPE,
                                            env=env,
                                            cwd=cwd)
    proc = yield from create

    tasks = [asyncio.async(log_stream(trial._stdout, proc.stdout, capture)),
             asyncio.async(log_stream(trial._stderr, proc.stderr, capture)),
             asyncio.async(proc.wait())]

    update_task = asyncio.async(update(trial, sleep_time=sleep_time))

    yield from asyncio.wait(tasks)

    update_task.cancel()

    return proc.returncode
