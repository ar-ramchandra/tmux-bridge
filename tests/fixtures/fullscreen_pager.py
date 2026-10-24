"""Offline full-screen transcript with no mouse reporting or tmux history."""
import os
import tty

tty.setraw(0)
os.write(1, b'\x1b[?1049h\x1b[?1000l\x1b[?1002l\x1b[?1003l')
page = arrows = 0
draft = ''
pending = b''


def draw():
    text = '\x1b[2J\x1b[HAPPLICATION_PAGE=%d\r\nARROWS=%d\r\nDRAFT=%s\r\n' % (page, arrows, draft)
    text += ('Older transcript content' if page else 'Latest transcript content') + '\r\nPAGER_READY'
    os.write(1, text.encode())


draw()
while True:
    data = os.read(0, 4096)
    if not data:
        break
    pending += data
    while pending:
        if pending.startswith(b'\x1b'):
            keys = {b'\x1b[5~': 'up', b'\x1b[6~': 'down', b'\x1b[A': 'arrow',
                    b'\x1b[B': 'arrow', b'\x1bOA': 'arrow', b'\x1bOB': 'arrow'}
            key = next((key for key in keys if pending.startswith(key)), None)
            if key is None and any(key.startswith(pending) for key in keys):
                break
            if key:
                action = keys[key]
                if action == 'up': page += 1
                elif action == 'down': page = max(0, page - 1)
                else: arrows += 1
                pending = pending[len(key):]
            else:
                pending = pending[1:]
        else:
            char, pending = pending[0], pending[1:]
            if 32 <= char < 127:
                draft += chr(char)
        draw()
