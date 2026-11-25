import io
import os
import unittest
from unittest.mock import Mock, patch

from sources import gbl_home_recovery, remote_target


class RemoteReplyTests(unittest.TestCase):
    def target(self, replies):
        proc = Mock(stdin=io.StringIO(), stdout=io.StringIO(replies))
        proc.poll.return_value = None
        return remote_target.RemoteTarget("example-phone", "example-peer", "android", proc), proc

    def test_mismatched_reply_closes_the_bridge(self):
        target, proc = self.target('{"id":0,"ok":true}\n')
        with self.assertRaisesRegex(gbl_home_recovery.HomeRecoveryError, "Unexpected reply ID"):
            target._call_sync({"cmd": "tap"})
        with self.assertRaisesRegex(gbl_home_recovery.HomeRecoveryError, "bridge is closed"):
            target._call_sync({"cmd": "tap"})
        self.assertTrue(proc.stdin.closed)
        proc.wait.assert_called_once_with(timeout=5)

    def test_timed_out_reply_closes_the_bridge(self):
        target, proc = self.target("")
        timeout = gbl_home_recovery.HomeRecoveryError("timed out")
        with patch.object(remote_target, "_readline_timeout", side_effect=timeout):
            with self.assertRaisesRegex(gbl_home_recovery.HomeRecoveryError, "timed out"):
                target._call_sync({"cmd": "tap"})
        self.assertTrue(proc.stdout.closed)
        with self.assertRaisesRegex(gbl_home_recovery.HomeRecoveryError, "bridge is closed"):
            target._call_sync({"cmd": "tap"})

    def test_matching_command_error_keeps_the_bridge_synchronized(self):
        target, _proc = self.target('{"id":1,"ok":false,"error":"screen unavailable"}\n'
                                    '{"id":2,"ok":true}\n')
        with self.assertRaisesRegex(gbl_home_recovery.HomeRecoveryError, "screen unavailable"):
            target._call_sync({"cmd": "screenshot"})
        self.assertTrue(target._call_sync({"cmd": "tap"})["ok"])

    def test_partial_reply_still_obeys_the_deadline(self):
        readable, writable = os.pipe()
        try:
            with os.fdopen(readable) as stream:
                os.write(writable, b'{"id":1')
                with self.assertRaisesRegex(gbl_home_recovery.HomeRecoveryError, "timed out"):
                    remote_target._readline_timeout(stream, 0.02)
        finally:
            os.close(writable)
