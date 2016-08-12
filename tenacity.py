# Copyright 2016 Julien Danjou
# Copyright 2016 Joshua Harlow
# Copyright 2013-2014 Ray Holder
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import random
import six
import sys
import time
import traceback


# sys.maxint / 2, since Python 3.2 doesn't have a sys.maxint...
try:
    MAX_WAIT = sys.maxint / 2
except AttributeError:
    MAX_WAIT = 1073741823


def _retry_if_exception_of_type(retryable_types):
    def _retry_if_exception_these_types(exception):
        return isinstance(exception, retryable_types)
    return _retry_if_exception_these_types


def retry(*dargs, **dkw):
    """Decorator function that instantiates the Retrying object.

    @param *dargs: positional arguments passed to Retrying object
    @param **dkw: keyword arguments passed to the Retrying object
    """
    # support both @retry and @retry() as valid syntax
    if len(dargs) == 1 and callable(dargs[0]):
        def wrap_simple(f):

            @six.wraps(f)
            def wrapped_f(*args, **kw):
                return Retrying().call(f, *args, **kw)

            return wrapped_f

        return wrap_simple(dargs[0])

    else:
        def wrap(f):

            @six.wraps(f)
            def wrapped_f(*args, **kw):
                return Retrying(*dargs, **dkw).call(f, *args, **kw)

            return wrapped_f

        return wrap


class stop_after_attempt(object):
    """Strategy that stops when the previous attempt >= max_attempt."""

    def __init__(self, max_attempt_number):
        self.max_attempt_number = max_attempt_number

    def __call__(self, previous_attempt_number, delay_since_first_attempt_ms):
        return previous_attempt_number >= self.max_attempt_number


class stop_after_delay(object):
    """Strategy that stops when the time from the first attempt >= limit."""

    def __init__(self, max_delay):
        self.max_delay = max_delay

    def __call__(self, previous_attempt_number, delay_since_first_attempt_ms):
        return delay_since_first_attempt_ms >= self.max_delay


class wait_fixed(object):
    """Wait strategy that waits a fixed amount of time between each retry."""

    def __init__(self, wait):
        self.wait_fixed = wait

    def __call__(self, previous_attempt_number, delay_since_first_attempt_ms):
        return self.wait_fixed


class wait_none(wait_fixed):
    """Wait strategy that doesn't wait at all before retrying."""

    def __init__(self):
        super(wait_none, self).__init__(0)


class wait_random(object):
    """Wait strategy that waits a random amount of time between min/max."""

    def __init__(self, min=0, max=1000):
        self.wait_random_min = min
        self.wait_random_max = max

    def __call__(self, previous_attempt_number, delay_since_first_attempt_ms):
        return random.randint(self.wait_random_min, self.wait_random_max)


class wait_incrementing(object):
    """Wait an incremental amount of time after each attempt.

    Starting at a starting value and incrementing by a value for each attempt
    (and restricting the upper limit to some maximum value).
    """

    def __init__(self, start=0, increment=100, max=MAX_WAIT):
        self.start = start
        self.increment = increment
        self.max = max

    def __call__(self, previous_attempt_number, delay_since_first_attempt_ms):
        result = self.start + (
            self.increment * (previous_attempt_number - 1)
        )
        if result > self.max:
            result = self.max
        if result < 0:
            result = 0
        return result


class wait_exponential(object):
    """Wait strategy that applies exponential backoff.

    It allows for a customized multiplier and an ability to restrict the
    upper limit to some maximum value.
    """

    #: Defaults to 2^n (where n is the prior attempt number/count).
    EXP_BASE = 2

    def __init__(self, multiplier=1, max=MAX_WAIT):
        self.multiplier = multiplier
        self.max = max

    def __call__(self, previous_attempt_number, delay_since_first_attempt_ms):
        exp = self.EXP_BASE ** previous_attempt_number
        result = self.multiplier * exp
        if result > self.max:
            result = self.max
        if result < 0:
            result = 0
        return result


def _never_reject(result):
    """Rejection strategy that never rejects any result."""
    return False


def _always_reject(result):
    """Rejection strategy that always rejects any result."""
    return True


class _MaybeReject(object):
    """Rejection strategy that proxies to 2 functions to reject (or not)."""

    def __init__(self, retry_on_exception=None,
                 retry_on_result=None):
        self.retry_on_exception = retry_on_exception
        self.retry_on_result = retry_on_result

    def __call__(self, attempt):
        reject = False
        if attempt.has_exception:
            if self.retry_on_exception is not None:
                reject |= self.retry_on_exception(attempt.value[1])
        else:
            if self.retry_on_result is not None:
                reject |= self.retry_on_result(attempt.value)
        return reject


class Retrying(object):
    """Retrying controller."""

    def __init__(self,
                 stop=None, wait=wait_none(),
                 retry_on_exception=None,
                 retry_on_result=None,
                 wrap_exception=False,
                 wait_jitter_max=None,
                 before_attempts=None,
                 after_attempts=None):
        self.stop = stop
        self.wait = wait
        self.should_reject = self._select_reject_strategy(
            retry_on_exception=retry_on_exception,
            retry_on_result=retry_on_result)
        self._wrap_exception = wrap_exception
        self._before_attempts = before_attempts
        self._after_attempts = after_attempts
        self._wait_jitter_max = wait_jitter_max

    @staticmethod
    def _select_reject_strategy(retry_on_exception=None, retry_on_result=None):
        if retry_on_exception is None:
            retry_on_exception = _always_reject
        else:
            if isinstance(retry_on_exception, (tuple)):
                retry_on_exception = _retry_if_exception_of_type(
                    retry_on_exception)
        if retry_on_result is None:
            retry_on_result = _never_reject
        return _MaybeReject(retry_on_result=retry_on_result,
                            retry_on_exception=retry_on_exception)

    def call(self, fn, *args, **kwargs):
        start_time = int(round(time.time() * 1000))
        attempt_number = 1
        while True:
            if self._before_attempts:
                self._before_attempts(attempt_number)

            try:
                attempt = Attempt(fn(*args, **kwargs), attempt_number, False)
            except Exception:
                tb = sys.exc_info()
                attempt = Attempt(tb, attempt_number, True)

            if not self.should_reject(attempt):
                return attempt.get(self._wrap_exception)

            if self._after_attempts:
                self._after_attempts(attempt_number)

            delay_since_first_attempt_ms = int(
                round(time.time() * 1000)
            ) - start_time
            if self.stop and self.stop(
                    attempt_number, delay_since_first_attempt_ms):
                if not self._wrap_exception and attempt.has_exception:
                    # get() on an attempt with an exception should cause it
                    # to be raised, but raise just in case
                    raise attempt.get()
                else:
                    raise RetryError(attempt)
            else:
                if self.wait:
                    sleep = self.wait(
                        attempt_number, delay_since_first_attempt_ms)
                else:
                    sleep = 0
                if self._wait_jitter_max:
                    jitter = random.random() * self._wait_jitter_max
                    sleep = sleep + max(0, jitter)
                time.sleep(sleep / 1000.0)

            attempt_number += 1


class Attempt(object):
    """Encapsulates a call to a target function.

    That may end as a normal return value from the function or an Exception
    depending on what occurred during the execution.
    """

    def __init__(self, value, attempt_number, has_exception):
        self.value = value
        self.attempt_number = attempt_number
        self.has_exception = has_exception

    def get(self, wrap_exception=False):
        """Return the return value of this Attempt instance or raise.

        If wrap_exception is true, this Attempt is wrapped inside of a
        RetryError before being raised.
        """
        if self.has_exception:
            if wrap_exception:
                raise RetryError(self)
            else:
                six.reraise(self.value[0], self.value[1], self.value[2])
        else:
            return self.value

    def __repr__(self):
        if self.has_exception:
            return "Attempts: {0}, Error:\n{1}".format(
                self.attempt_number,
                "".join(traceback.format_tb(self.value[2])))
        else:
            return "Attempts: {0}, Value: {1}".format(
                self.attempt_number, self.value)


class RetryError(Exception):
    "Encapsulates the last Attempt instance right before giving up"

    def __init__(self, last_attempt):
        self.last_attempt = last_attempt

    def __str__(self):
        return "RetryError[{0}]".format(self.last_attempt)