import code


class SubInterpreterExit(SystemExit):
    pass


def console_quit():
    raise SubInterpreterExit()


def run_python_console(britney_obj):
    console_locals = {
        'britney': britney_obj,
        '__name__': '__console__',
        '__doc__': None,
        'quit': console_quit,
        'exit': console_quit,
    }
    console = code.InteractiveConsole(locals=console_locals)
    banner = """\
Interactive python (REPL) shell in britney.

Locals available
 * britney: Instance of the Britney object.
 * quit()/exit(): leave this REPL console.
"""
    try:
        console.interact(banner=banner, exitmsg='')
    except SubInterpreterExit:
        pass
