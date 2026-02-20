import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
from types import SimpleNamespace

import bot_with_check_updated2 as bot


class DummyState:
    def __init__(self):
        self.cleared = 0

    async def clear(self):
        self.cleared += 1


class DummyMessage:
    def __init__(self, text: str = ""):
        self.text = text
        self.chat = SimpleNamespace(id=123)
        self.answers: list[tuple[str, object | None]] = []

    async def answer(self, text: str, reply_markup=None):
        self.answers.append((text, reply_markup))


class DummyCallback:
    def __init__(self, data: str, message: DummyMessage):
        self.data = data
        self.message = message
        self.answered = False

    async def answer(self):
        self.answered = True


def test_mode_command_clears_state_and_shows_keyboard():
    state = DummyState()
    msg = DummyMessage()

    asyncio.run(bot.mode_command(msg, state))

    assert state.cleared == 1
    assert msg.answers
    assert "Выберите режим" in msg.answers[-1][0]


def test_set_mode_light_clears_state_and_starts_light(monkeypatch):
    state = DummyState()
    msg = DummyMessage("Light")

    calls = {"set_user_mode": [], "light_start": 0}

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    def fake_set_user_mode(db_path, chat_id, mode):
        calls["set_user_mode"].append((db_path, chat_id, mode))

    async def fake_light_start(message, fsm_state):
        calls["light_start"] += 1

    monkeypatch.setattr(bot.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(bot, "set_user_mode", fake_set_user_mode)
    monkeypatch.setattr(bot, "light_start", fake_light_start)

    asyncio.run(bot.set_mode(msg, state))

    assert state.cleared == 1
    assert calls["set_user_mode"] and calls["set_user_mode"][-1][2] == "light"
    assert calls["light_start"] == 1
    assert any("Режим установлен" in txt for txt, _ in msg.answers)
    assert not any("Expert режим активирован" in txt for txt, _ in msg.answers)


def test_set_mode_expert_clears_state_and_sends_instruction(monkeypatch):
    state = DummyState()
    msg = DummyMessage("Expert")

    calls = {"set_user_mode": [], "light_start": 0}

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    def fake_set_user_mode(db_path, chat_id, mode):
        calls["set_user_mode"].append((db_path, chat_id, mode))

    async def fake_light_start(message, fsm_state):
        calls["light_start"] += 1

    monkeypatch.setattr(bot.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(bot, "set_user_mode", fake_set_user_mode)
    monkeypatch.setattr(bot, "light_start", fake_light_start)

    asyncio.run(bot.set_mode(msg, state))

    assert state.cleared == 1
    assert calls["set_user_mode"] and calls["set_user_mode"][-1][2] == "expert"
    assert calls["light_start"] == 0
    assert any("Expert режим активирован" in txt for txt, _ in msg.answers)


def test_switch_mode_callback_light_resets_state_and_starts_light(monkeypatch):
    state = DummyState()
    msg = DummyMessage()
    callback = DummyCallback("switch:light", msg)

    calls = {"set_user_mode": [], "light_start": 0}

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    def fake_set_user_mode(db_path, chat_id, mode):
        calls["set_user_mode"].append((db_path, chat_id, mode))

    async def fake_light_start(message, fsm_state):
        calls["light_start"] += 1

    monkeypatch.setattr(bot.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(bot, "set_user_mode", fake_set_user_mode)
    monkeypatch.setattr(bot, "light_start", fake_light_start)

    asyncio.run(bot.switch_mode_cb(callback, state))

    assert callback.answered is True
    assert state.cleared == 1
    assert calls["set_user_mode"] and calls["set_user_mode"][-1][2] == "light"
    assert calls["light_start"] == 1


def test_assess_expert_input_smoke():
    assert bot.assess_expert_input("Компрессорный холодильник бытовой, объём 300 л, температура -18") is True
    assert bot.assess_expert_input("холодильник") is False
