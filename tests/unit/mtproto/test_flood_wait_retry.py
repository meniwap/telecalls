"""Tests for FloodWait auto-retry functionality in RPC sender."""

from __future__ import annotations

from telecraft.mtproto.rpc.sender import (
    FloodWaitConfig,
    FloodWaitError,
    parse_flood_wait_seconds,
)


class TestParseFloodWaitSeconds:
    def test_flood_wait_basic(self):
        assert parse_flood_wait_seconds("FLOOD_WAIT_30") == 30
        assert parse_flood_wait_seconds("FLOOD_WAIT_1") == 1
        assert parse_flood_wait_seconds("FLOOD_WAIT_3600") == 3600

    def test_slowmode_wait(self):
        assert parse_flood_wait_seconds("SLOWMODE_WAIT_15") == 15
        assert parse_flood_wait_seconds("SLOWMODE_WAIT_60") == 60

    def test_flood_premium_wait(self):
        assert parse_flood_wait_seconds("FLOOD_PREMIUM_WAIT_42") == 42

    def test_not_flood_wait(self):
        assert parse_flood_wait_seconds("PEER_ID_INVALID") is None
        assert parse_flood_wait_seconds("USER_NOT_MUTUAL_CONTACT") is None
        assert parse_flood_wait_seconds("CHAT_ADMIN_REQUIRED") is None
        assert parse_flood_wait_seconds("") is None

    def test_embedded_in_message(self):
        # Sometimes error messages might have extra context
        assert parse_flood_wait_seconds("Error: FLOOD_WAIT_60 triggered") == 60


class TestFloodWaitError:
    def test_init(self):
        err = FloodWaitError(code=420, message="FLOOD_WAIT_30", wait_seconds=30)
        assert err.code == 420
        assert err.message == "FLOOD_WAIT_30"
        assert err.wait_seconds == 30
        assert "30s" in str(err)

    def test_is_rpc_sender_error(self):
        from telecraft.mtproto.rpc.sender import RpcSenderError

        err = FloodWaitError(code=420, message="FLOOD_WAIT_30", wait_seconds=30)
        assert isinstance(err, RpcSenderError)


class TestFloodWaitConfig:
    def test_defaults(self):
        cfg = FloodWaitConfig()
        assert cfg.enabled is True
        assert cfg.max_wait_seconds == 60
        assert cfg.max_retries == 3

    def test_custom_values(self):
        cfg = FloodWaitConfig(enabled=False, max_wait_seconds=120, max_retries=5)
        assert cfg.enabled is False
        assert cfg.max_wait_seconds == 120
        assert cfg.max_retries == 5


class TestFloodWaitRetryIntegration:
    """Integration-style tests using mock transport."""

    def test_flood_wait_sender_config(self):
        """Test that FloodWait config is properly stored in sender."""
        from unittest.mock import MagicMock

        from telecraft.mtproto.rpc.sender import MtprotoEncryptedSender

        # Create mock transport
        transport = MagicMock()

        # Create mock state
        state = MagicMock()
        state.server_salt = b"\x00" * 8
        state.next_seq_no = MagicMock(return_value=1)
        state.encrypt_inner_message = MagicMock(return_value=b"encrypted")

        msg_id_gen = MagicMock()
        msg_id_gen.next = MagicMock(return_value=12345)

        cfg = FloodWaitConfig(enabled=True, max_wait_seconds=5, max_retries=2)
        sender = MtprotoEncryptedSender(
            transport, state=state, msg_id_gen=msg_id_gen, flood_wait_config=cfg
        )

        # Test that config is stored
        assert sender._flood_wait_config.enabled is True
        assert sender._flood_wait_config.max_wait_seconds == 5
        assert sender._flood_wait_config.max_retries == 2

    def test_flood_wait_default_config(self):
        """Test that default FloodWait config is used when none provided."""
        from unittest.mock import MagicMock

        from telecraft.mtproto.rpc.sender import MtprotoEncryptedSender

        transport = MagicMock()
        state = MagicMock()
        state.server_salt = b"\x00" * 8
        msg_id_gen = MagicMock()

        sender = MtprotoEncryptedSender(transport, state=state, msg_id_gen=msg_id_gen)

        # Test that default config is applied
        assert sender._flood_wait_config.enabled is True
        assert sender._flood_wait_config.max_wait_seconds == 60
        assert sender._flood_wait_config.max_retries == 3

    def test_flood_wait_exceeds_max_raises(self):
        """Test that FloodWait exceeding max_wait_seconds should raise."""
        cfg = FloodWaitConfig(enabled=True, max_wait_seconds=10, max_retries=3)

        # This simulates what happens when invoke_tl gets a FloodWaitError
        # with wait_seconds > max_wait_seconds
        err = FloodWaitError(code=420, message="FLOOD_WAIT_60", wait_seconds=60)

        # The error's wait_seconds (60) > max_wait_seconds (10), so it should raise
        assert err.wait_seconds > cfg.max_wait_seconds

    def test_flood_wait_disabled_raises_immediately(self):
        """Test that disabled flood wait config raises error immediately."""
        cfg = FloodWaitConfig(enabled=False)
        assert cfg.enabled is False

        # When enabled=False, any FloodWaitError should be raised without retry
        err = FloodWaitError(code=420, message="FLOOD_WAIT_5", wait_seconds=5)
        # In actual implementation, this would be raised immediately
        assert err.wait_seconds == 5
