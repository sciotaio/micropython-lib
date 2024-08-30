# MIT license; Copyright (c) 2022 Jim Mussared

import micropython
from micropython import const
import re
import sys
import time
import asyncio
import io
import os

# Import statement (needs to be global, and does not return).
_RE_IMPORT = re.compile("^import ([^ ]+)( as ([^ ]+))?")
_RE_FROM_IMPORT = re.compile("^from [^ ]+ import ([^ ]+)( as ([^ ]+))?")
# Global variable assignment.
_RE_GLOBAL = re.compile("^([a-zA-Z0-9_]+) ?=[^=]")
# General assignment expression or import statement (does not return a value).
_RE_ASSIGN = re.compile("[^=]=[^=]")

# Command hist (One reserved slot for the current command).
_HISTORY_LIMIT = const(5 + 1)


CHAR_CTRL_A = const(1)
CHAR_CTRL_B = const(2)
CHAR_CTRL_C = const(3)
CHAR_CTRL_D = const(4)
CHAR_CTRL_E = const(5)

# Needed to enable terminal duplication with os.dupterm
class OutputStream(io.IOBase):
    def __init__(self, stream):
        self.stream = stream
        
    def write(self, data):
        self.stream.write(data)
        
    def readinto(self, buf):
        return None                


async def execute(code, g, in_stream, out_stream):
    if not code.strip():
        return

    try:
        if "await " in code:
            # Execute the code snippet in an async context.
            if m := _RE_IMPORT.match(code) or _RE_FROM_IMPORT.match(code):
                code = "global {}\n    {}".format(m.group(3) or m.group(1), code)
            elif m := _RE_GLOBAL.match(code):
                code = "global {}\n    {}".format(m.group(1), code)
            elif not _RE_ASSIGN.search(code):
                code = "return {}".format(code)

            code = """
import asyncio
async def __code():
    {}

__exec_task = asyncio.create_task(__code())
""".format(code)

            async def kbd_intr_task(exec_task, in_stream):
                while True:
                    if ord(await in_stream.read(1)) == CHAR_CTRL_C:
                        exec_task.cancel()
                        return

            l = {"__exec_task": None}
            exec(code, g, l)
            exec_task = l["__exec_task"]

            # Concurrently wait for either Ctrl-C from the stream or task
            # completion.
            intr_task = asyncio.create_task(kbd_intr_task(exec_task, in_stream))

            prev = None
            if out_stream != sys.stdout:
                prev = os.dupterm(out_stream)
                
            try:
                try:
                    return await exec_task
                except asyncio.CancelledError:
                    pass
            finally:
                os.dupterm(prev)
                        
                intr_task.cancel()
                try:
                    await intr_task
                except asyncio.CancelledError:
                    pass
        else:
            # Excute code snippet directly.
            prev = None
            if out_stream != sys.stdout:
                prev = os.dupterm(out_stream)
            
            try:
                try:
                    micropython.kbd_intr(3)
                    try:
                        return eval(code, g)
                    except SyntaxError:
                        # Maybe an assignment, try with exec.
                        return exec(code, g)
                except KeyboardInterrupt:
                    pass
            finally:
                os.dupterm(prev)
                micropython.kbd_intr(-1)

    except Exception as err:
        print("{}: {}".format(type(err).__name__, err))


# REPL task. Invoke this with an optional mutable globals dict.
# The in_stream should be an object that has an async read method
# The outstream should be an object that has a (non-async) write method
async def task(in_stream=None, out_stream=None, g=None, prompt="--> "):
    print("Starting asyncio REPL...")
    if g is None:
        g = __import__("__main__").__dict__
        
    if in_stream is None:
        in_stream = asyncio.StreamReader(sys.stdin)
        
    if out_stream is None:
        out_stream = sys.stdout
    else:
        out_stream = OutputStream(out_stream)
        
    try:
        micropython.kbd_intr(-1)
        # clear = True
        hist = [None] * _HISTORY_LIMIT
        hist_i = 0  # Index of most recent entry.
        hist_n = 0  # Number of history entries.
        c = 0  # ord of most recent character.
        t = 0  # timestamp of most recent character.
        while True:
            hist_b = 0  # How far back in the history are we currently.
            out_stream.write(prompt)
            cmd: str = ""
            paste = False
            curs = 0  # cursor offset from end of cmd buffer
            while True:
                b = await in_stream.read(1)
                pc = c  # save previous character
                c = ord(b)
                pt = t  # save previous time
                t = time.ticks_ms()
                if c < 0x20 or c > 0x7E:
                    if c == 0x0A:
                        # LF
                        if paste:
                            out_stream.write(b)
                            cmd += b
                            continue
                        # If the previous character was also LF, and was less
                        # than 20 ms ago, this was likely due to CRLF->LFLF
                        # conversion, so ignore this linefeed.
                        if pc == 0x0A and time.ticks_diff(t, pt) < 20:
                            continue
                        if curs:
                            # move cursor to end of the line
                            out_stream.write("\x1B[{}C".format(curs))
                            curs = 0
                        out_stream.write("\n")
                        if cmd:
                            # Push current command.
                            hist[hist_i] = cmd
                            # Increase history length if possible, and rotate ring forward.
                            hist_n = min(_HISTORY_LIMIT - 1, hist_n + 1)
                            hist_i = (hist_i + 1) % _HISTORY_LIMIT

                            result = await execute(cmd, g, in_stream, out_stream)
                            if result is not None:
                                out_stream.write(repr(result))
                                out_stream.write("\n")
                        break
                    elif c == 0x08 or c == 0x7F:
                        # Backspace.
                        if cmd:
                            if curs:
                                cmd = "".join((cmd[: -curs - 1], cmd[-curs:]))
                                out_stream.write(
                                    "\x08\x1B[K"
                                )  # move cursor back, erase to end of line
                                out_stream.write(cmd[-curs:])  # redraw line
                                out_stream.write("\x1B[{}D".format(curs))  # reset cursor location
                            else:
                                cmd = cmd[:-1]
                                out_stream.write("\x08 \x08")
                    elif c == CHAR_CTRL_A:
                        continue
                    elif c == CHAR_CTRL_B:
                        continue
                    elif c == CHAR_CTRL_C:
                        if paste:
                            break
                        out_stream.write("\n")
                        break
                    elif c == CHAR_CTRL_D:
                        if paste:
                            result = await execute(cmd, g, in_stream, out_stream)
                            if result is not None:
                                out_stream.write(repr(result))
                                out_stream.write("\n")
                            break

                        out_stream.write("\n")
                        # Shutdown asyncio.
                        asyncio.new_event_loop()
                        return
                    elif c == CHAR_CTRL_E:
                        out_stream.write("paste mode; Ctrl-C to cancel, Ctrl-D to finish\n===\n")
                        paste = True
                    elif c == 0x1B:
                        # Start of escape sequence.
                        key = await in_stream.read(2)
                        if key in ("[A", "[B"):  # up, down
                            # Stash the current command.
                            hist[(hist_i - hist_b) % _HISTORY_LIMIT] = cmd
                            # Clear current command.
                            b = "\x08" * len(cmd)
                            out_stream.write(b)
                            out_stream.write(" " * len(cmd))
                            out_stream.write(b)
                            # Go backwards or forwards in the history.
                            if key == "[A":
                                hist_b = min(hist_n, hist_b + 1)
                            else:
                                hist_b = max(0, hist_b - 1)
                            # Update current command.
                            cmd = hist[(hist_i - hist_b) % _HISTORY_LIMIT]
                            out_stream.write(cmd)
                        elif key == "[D":  # left
                            if curs < len(cmd) - 1:
                                curs += 1
                                out_stream.write("\x1B")
                                out_stream.write(key)
                        elif key == "[C":  # right
                            if curs:
                                curs -= 1
                                out_stream.write("\x1B")
                                out_stream.write(key)
                        elif key == "[H":  # home
                            pcurs = curs
                            curs = len(cmd)
                            out_stream.write("\x1B[{}D".format(curs - pcurs))  # move cursor left
                        elif key == "[F":  # end
                            pcurs = curs
                            curs = 0
                            out_stream.write("\x1B[{}C".format(pcurs))  # move cursor right
                    else:
                        # out_stream.write("\\x")
                        # out_stream.write(hex(c))
                        pass
                else:
                    if curs:
                        # inserting into middle of line
                        cmd = "".join((cmd[:-curs], b, cmd[-curs:]))
                        out_stream.write(cmd[-curs - 1 :])  # redraw line to end
                        out_stream.write("\x1B[{}D".format(curs))  # reset cursor location
                    else:
                        out_stream.write(b)
                        cmd += b
    finally:
        micropython.kbd_intr(3)
