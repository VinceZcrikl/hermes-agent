from hermes_cli import cli_output


class _TTY:
    def isatty(self):
        return True


class _Pipe:
    def isatty(self):
        return False


def test_password_prompt_uses_prompt_toolkit_on_windows_tty(monkeypatch):
    captured = {}

    def fake_prompt(message, is_password=False, key_bindings=None):
        captured["message"] = message
        captured["is_password"] = is_password
        captured["key_bindings"] = key_bindings
        return "secret-token"

    monkeypatch.setattr(cli_output.sys, "platform", "win32", raising=False)
    monkeypatch.setattr(cli_output.sys, "stdin", _TTY())
    monkeypatch.setattr(cli_output.sys, "stdout", _TTY())
    monkeypatch.setattr("prompt_toolkit.shortcuts.prompt", fake_prompt)

    value = cli_output.prompt("Bot token", password=True)

    assert value == "secret-token"
    assert captured["is_password"] is True
    assert captured["key_bindings"] is not None


def test_password_prompt_falls_back_to_getpass_without_tty(monkeypatch):
    monkeypatch.setattr(cli_output.sys, "platform", "win32", raising=False)
    monkeypatch.setattr(cli_output.sys, "stdin", _Pipe())
    monkeypatch.setattr(cli_output.sys, "stdout", _TTY())
    monkeypatch.setattr(
        "getpass.getpass",
        lambda _prompt="": "\x1b[200~secret-token\x1b[201~",
    )

    value = cli_output.prompt("Bot token", password=True)

    assert value == "secret-token"