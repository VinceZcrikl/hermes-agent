"""Shared CLI output helpers for Hermes CLI modules.

Extracts the identical ``print_info/success/warning/error`` and ``prompt()``
functions previously duplicated across setup.py, tools_config.py,
mcp_config.py, and memory_setup.py.
"""

import getpass
import re
import sys

from hermes_cli.colors import Colors, color


# ─── Print Helpers ────────────────────────────────────────────────────────────


def print_info(text: str) -> None:
    """Print a dim informational message."""
    print(color(f"  {text}", Colors.DIM))


def print_success(text: str) -> None:
    """Print a green success message with ✓ prefix."""
    print(color(f"✓ {text}", Colors.GREEN))


def print_warning(text: str) -> None:
    """Print a yellow warning message with ⚠ prefix."""
    print(color(f"⚠ {text}", Colors.YELLOW))


def print_error(text: str) -> None:
    """Print a red error message with ✗ prefix."""
    print(color(f"✗ {text}", Colors.RED))


def print_header(text: str) -> None:
    """Print a bold yellow header."""
    print(color(f"\n  {text}", Colors.YELLOW))


# ─── Input Prompts ────────────────────────────────────────────────────────────


_BRACKETED_PASTE_PATTERN = re.compile(r"\x1b\[\s*200~|\x1b\[\s*201~")


def _sanitize_pasted_input(value: str) -> str:
    """Strip leaked bracketed-paste control markers from pasted text."""
    if not isinstance(value, str) or not value:
        return value
    return _BRACKETED_PASTE_PATTERN.sub("", value)


def _stream_is_tty(stream: object) -> bool:
    try:
        return bool(stream and stream.isatty())
    except Exception:
        return False


def _should_use_prompt_toolkit_secret_input() -> bool:
    """Use prompt_toolkit only for interactive Windows secret prompts.

    The setup/gateway wizard still runs as a plain terminal flow, but on
    Windows prompt_toolkit gives us a hidden-input prompt with paste support.
    """
    return (
        sys.platform == "win32"
        and _stream_is_tty(getattr(sys, "stdin", None))
        and _stream_is_tty(getattr(sys, "stdout", None))
    )


def _build_secret_prompt_bindings():
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys

    kb = KeyBindings()

    @kb.add("c-v")
    def _paste_clipboard(event):
        try:
            clip = event.app.clipboard.get_data()
        except Exception:
            return
        text = _sanitize_pasted_input(getattr(clip, "text", "") or "")
        if text:
            event.current_buffer.insert_text(text)

    @kb.add(Keys.BracketedPaste, eager=True)
    def _handle_bracketed_paste(event):
        text = _sanitize_pasted_input(event.data or "")
        if text:
            event.current_buffer.insert_text(text)

    return kb


def read_line(display: str) -> str:
    """Read a visible line and strip leaked bracketed-paste markers."""
    return _sanitize_pasted_input(input(display))


def read_secret_line(display: str) -> str:
    """Read a hidden line, preferring a Windows paste-friendly prompt."""
    if _should_use_prompt_toolkit_secret_input():
        try:
            from prompt_toolkit.formatted_text import ANSI
            from prompt_toolkit.shortcuts import prompt as pt_prompt

            return _sanitize_pasted_input(
                pt_prompt(
                    ANSI(display),
                    is_password=True,
                    key_bindings=_build_secret_prompt_bindings(),
                )
            )
        except ImportError:
            pass

    return _sanitize_pasted_input(getpass.getpass(display))


def prompt(
    question: str,
    default: str | None = None,
    password: bool = False,
) -> str:
    """Prompt the user for input with optional default and password masking.

    Replaces the four independent ``_prompt()`` / ``prompt()`` implementations
    in setup.py, tools_config.py, mcp_config.py, and memory_setup.py.

    Returns the user's input (stripped), or *default* if the user presses Enter.
    Returns empty string on Ctrl-C or EOF.
    """
    suffix = f" [{default}]" if default else ""
    display = color(f"  {question}{suffix}: ", Colors.YELLOW)

    try:
        if password:
            value = read_secret_line(display)
        else:
            value = read_line(display)
        value = value.strip()
        return value if value else (default or "")
    except (KeyboardInterrupt, EOFError):
        print()
        return ""


def prompt_yes_no(question: str, default: bool = True) -> bool:
    """Prompt for a yes/no answer. Returns bool."""
    hint = "Y/n" if default else "y/N"
    answer = prompt(f"{question} ({hint})")
    if not answer:
        return default
    return answer.lower().startswith("y")
