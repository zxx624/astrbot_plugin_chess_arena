import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import main as plugin_mod


def make_plugin():
    plugin = object.__new__(plugin_mod.ChessArenaPlugin)
    plugin.config = {}
    plugin.token = "bot-token"
    plugin.bot_name = "PublicBot"
    plugin.server_profile = {"bot_id": "bot-1", "name": "PublicBot"}
    plugin.enabled_games = ["xiangqi", "go", "doudizhu"]
    plugin.default_game = "xiangqi"
    plugin.cardroom_pool_bindings = []
    plugin.cardroom_seats = []
    plugin.cardroom_context_max_history = 6
    plugin.verbose_logging = False
    plugin._save_runtime_config = AsyncMock()
    plugin._card_tool_pool_status = AsyncMock(return_value="pool status")
    return plugin


class CardRoomPublicFlowTests(unittest.TestCase):
    def test_public_schema_defaults_use_domain_and_enable_three_capabilities(self):
        schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["arena_base"]["default"], "https://gulu624.icu")
        self.assertEqual(schema["card_arena_base"]["default"], "https://gulu624.icu")
        self.assertEqual(schema["cardroom_base_url"]["default"], "https://gulu624.icu")
        self.assertEqual(schema["enabled_games"]["default"], "xiangqi,go,doudizhu")
        self.assertTrue(schema["cardroom_enabled"]["default"])
        self.assertTrue(schema["cardroom_llm_decision_enabled"]["default"])

    def test_doudizhu_is_a_capability_but_not_a_duel_game(self):
        plugin = make_plugin()
        parsed = plugin._parse_enabled_games("xiangqi,go,doudizhu")
        self.assertEqual(parsed, ["xiangqi", "go", "doudizhu"])
        plugin.enabled_games = parsed
        self.assertEqual(plugin._normalize_game("doudizhu"), "xiangqi")
        self.assertEqual(plugin._normalize_game("go"), "go")

    def test_missing_enabled_games_uses_public_three_game_default(self):
        plugin = make_plugin()
        self.assertEqual(plugin._parse_enabled_games(None), ["xiangqi", "go", "doudizhu"])
        self.assertEqual(plugin._parse_enabled_games("  "), ["xiangqi", "go", "doudizhu"])

    def test_registration_payload_advertises_enabled_games(self):
        plugin = make_plugin()
        plugin._instance_name = lambda: "astrbot-test"
        payload = plugin._bot_settings_payload(include_client=True)
        self.assertEqual(payload["enabled_games"], ["xiangqi", "go", "doudizhu"])

    def test_sync_server_enabled_games_patches_exact_capabilities(self):
        plugin = make_plugin()
        plugin._request_text_with_fallback = AsyncMock(return_value=(
            "https://gulu624.icu",
            200,
            json.dumps({"enabled_games": ["xiangqi", "go", "doudizhu"]}),
        ))
        plugin._auth_headers = lambda: {"Authorization": "Bearer bot-token"}

        asyncio.run(plugin._sync_server_enabled_games({"enabled_games": ["xiangqi"]}))

        call = plugin._request_text_with_fallback.await_args
        self.assertEqual(call.args[:2], ("PATCH", "/api/bots/me"))
        self.assertEqual(call.kwargs["json_payload"], {
            "enabled_games": ["xiangqi", "go", "doudizhu"],
        })

    def test_prompt_uses_landlord_and_opponent_counts_from_private_players(self):
        plugin = make_plugin()
        session = plugin_mod.CardRoomDecisionSession(room_id="room-1", seat="0", persona="test")
        view = {
            "my_hand": ["3S", "3H"],
            "current_seat": "PublicBot",
            "last_play": None,
            "pass_count": 0,
            "players": [
                {"seat": 0, "hand_count": 20, "is_landlord": True, "is_me": True},
                {"seat": 1, "hand_count": 11, "is_landlord": False, "is_me": False},
                {"seat": 2, "hand_count": 7, "is_landlord": False, "is_me": False},
            ],
        }
        legal = {"can_pass": False, "candidate_groups": {"pairs": [["3S", "3H"]]}}

        prompt = plugin._build_cardroom_decision_prompt(session, view, legal)

        self.assertIn("地主 seat：seat0", prompt)
        self.assertIn("'seat1': 11", prompt)
        self.assertIn("'seat2': 7", prompt)
        self.assertNotIn("spectator", prompt.lower())

    def test_llm_candidates_reject_illegal_duplicate_ids_and_ignore_forged_cards(self):
        plugin = make_plugin()
        legal = {
            "can_pass": True,
            "candidate_groups": {
                "singles": ["3S"],
                "pairs": [["4S", "4H"]],
            },
        }

        resolved = plugin._resolve_cardroom_llm_candidates([
            {"action_id": "play:NOT_A_CARD", "cards": ["RJ"], "reason": "illegal"},
            {"action_id": "play:4S,4H", "cards": ["BJ", "RJ"], "reason": "valid id"},
            {"action_id": "play:4S,4H", "cards": ["3S"], "reason": "duplicate"},
            {"action_id": "pass", "cards": ["3S"], "reason": "forged pass cards"},
        ], legal)

        self.assertEqual([item["action_id"] for item in resolved], ["play:4S,4H", "pass"])
        self.assertEqual(resolved[0]["cards"], ["4S", "4H"])
        self.assertEqual(resolved[1]["cards"], [])

    def test_combination_first_fallback_plays_supported_multi_card_families(self):
        plugin = make_plugin()
        cases = [
            ("straights", ["3S", "4S", "5S", "6S", "7S"]),
            ("consecutive_pairs", ["3S", "3H", "4S", "4H", "5S", "5H"]),
            ("triple_with_single", ["6S", "6H", "6D", "3S"]),
            ("triple_with_pair", ["7S", "7H", "7D", "4S", "4H"]),
        ]
        for family, cards in cases:
            with self.subTest(family=family):
                legal = {"can_pass": False, "candidate_groups": {family: [cards]}}
                selected = plugin._select_cardroom_action({"last_play": None}, legal)
                self.assertEqual(selected["action"], "play")
                self.assertEqual(selected["cards"], cards)

    def test_persisted_pool_binding_is_restored_after_restart(self):
        plugin = make_plugin()
        raw = json.dumps([{
            "slot": 3,
            "controller_id": "bot-1",
            "seat": 2,
            "seat_token": "seat-secret",
            "room_id": "room-live",
            "status": "playing",
        }])

        restored = plugin._parse_cardroom_pool_bindings(raw)
        plugin.cardroom_pool_bindings = restored

        self.assertEqual(restored[0]["seat"], "2")
        self.assertEqual(restored[0]["room_id"], "room-live")
        self.assertEqual(plugin._active_cardroom_bindings(), [{
            "slot": 3,
            "room_id": "room-live",
            "seat": "2",
            "token": "seat-secret",
        }])

    def test_join_uses_bot_token_and_persists_waiting_binding(self):
        plugin = make_plugin()
        plugin._card_api_json = AsyncMock(return_value=(200, {
            "joined": True,
            "seat": {"seat": 1, "seat_id": "seat1"},
            "seat_token": "seat-secret",
            "room_id": None,
            "slot": {"slot": 2, "status": "waiting", "seats": []},
        }, "{}"))

        result = asyncio.run(plugin._card_tool_pool_join(2))

        plugin._card_api_json.assert_awaited_once_with(
            "POST",
            "/api/card-rooms/pool/2/join-token",
            json_payload={"token": "bot-token", "display_name": "PublicBot"},
        )
        self.assertNotIn("seat-secret", result)
        self.assertEqual(plugin.cardroom_pool_bindings, [{
            "slot": 2,
            "controller_id": "bot-1",
            "seat": "1",
            "seat_token": "seat-secret",
            "room_id": "",
            "status": "waiting",
        }])
        plugin._save_runtime_config.assert_awaited_once()

    def test_reconcile_updates_seat_and_room_after_auto_start(self):
        plugin = make_plugin()
        plugin.cardroom_pool_bindings = [{
            "slot": 2,
            "controller_id": "bot-1",
            "seat": "1",
            "seat_token": "seat-secret",
            "room_id": "",
            "status": "waiting",
        }]
        plugin._card_api_json = AsyncMock(return_value=(200, {"slots": [{
            "slot": 2,
            "status": "playing",
            "room_id": "room-live",
            "seats": [{"seat": 0, "controller_id": "bot-1"}],
        }]}, "{}"))

        asyncio.run(plugin._cardroom_reconcile_pool_bindings())

        self.assertEqual(plugin.cardroom_pool_bindings[0]["seat"], "0")
        self.assertEqual(plugin.cardroom_pool_bindings[0]["room_id"], "room-live")
        self.assertEqual(plugin.cardroom_pool_bindings[0]["status"], "playing")
        plugin._save_runtime_config.assert_awaited_once()

    def test_leave_uses_bot_token_and_removes_binding(self):
        plugin = make_plugin()
        plugin.cardroom_pool_bindings = [{
            "slot": 2,
            "controller_id": "bot-1",
            "seat": "0",
            "seat_token": "seat-secret",
            "room_id": "room-live",
            "status": "playing",
        }]
        plugin._card_api_json = AsyncMock(return_value=(200, {"left": True}, "{}"))

        asyncio.run(plugin._card_tool_pool_leave(2))

        plugin._card_api_json.assert_awaited_once_with(
            "POST",
            "/api/card-rooms/pool/2/leave-token",
            json_payload={"token": "bot-token"},
        )
        self.assertEqual(plugin.cardroom_pool_bindings, [])
        plugin._save_runtime_config.assert_awaited_once()

    def test_start_command_does_not_call_admin_endpoint(self):
        plugin = make_plugin()
        plugin._card_api_json = AsyncMock()

        result = asyncio.run(plugin._card_tool_pool_start(2))

        plugin._card_api_json.assert_not_awaited()
        self.assertIn("自动开局", result)

    def test_runtime_payload_contains_pool_bindings(self):
        plugin = make_plugin()
        plugin.cardroom_pool_bindings = [{
            "slot": 2,
            "controller_id": "bot-1",
            "seat": "0",
            "seat_token": "seat-secret",
            "room_id": "room-live",
            "status": "playing",
        }]
        plugin.arena_base = "https://gulu624.icu"
        plugin.arena_fallback_bases = []
        plugin.auto_register = True
        plugin.commentary_enabled = True
        plugin.commentary_timeout_sec = 8
        plugin.llm_provider_mode = "custom"
        plugin.llm_provider_id = "self/deepseek"
        plugin.llm_tools_enabled = True
        plugin.llm_tools_allow_actions = False
        plugin.auto_accept_challenges = True
        plugin.challenge_decision_mode = "auto_accept"
        plugin.server_challenge_policy = "auto_accept"
        plugin.owner_notify_enabled = True
        plugin.owner_notify_targets = ""
        plugin.owner_decision_timeout_sec = 180
        plugin.match_report_enabled = True
        plugin.engine_mode = "auto"
        plugin.engine_depth = 3
        plugin.engine_timeout_sec = 8
        plugin.custom_engine_command = ""
        plugin.custom_engine_http_url = ""
        plugin.custom_engine_http_headers = ""
        plugin.local_engine_node_path = "node"
        plugin.move_timeout_sec = 10
        plugin.announce_to_current_chat = False
        plugin.verbose_logging = False

        payload = plugin._runtime_config_payload("bot-token")

        self.assertEqual(json.loads(payload["cardroom_pool_bindings"])[0]["room_id"], "room-live")

    def test_runtime_save_only_writes_current_astrbot_instance(self):
        plugin = make_plugin()
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "astrbot1.json"
            other = Path(tmp) / "astrbot2.json"
            current.write_text('{"owner":"astrbot1"}', encoding="utf-8")
            other.write_text('{"owner":"astrbot2"}', encoding="utf-8")
            plugin._candidate_runtime_config_paths = lambda: [current, other]
            plugin._instance_runtime_config_path = lambda: current
            plugin._runtime_config_payload = lambda token: {"token": token, "cardroom_pool_bindings": "[]"}
            plugin._save_runtime_config = plugin_mod.ChessArenaPlugin._save_runtime_config.__get__(plugin)

            asyncio.run(plugin._save_runtime_config())

            self.assertEqual(json.loads(current.read_text(encoding="utf-8"))["token"], "bot-token")
            self.assertNotIn("token", json.loads(other.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
