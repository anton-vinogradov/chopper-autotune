"""What a fake Klipper server must refuse the way the real one does
(tests/test_klipper_contract.py holds this fake to the real parser)."""
import json
import re
import shlex

TRADITIONAL = re.compile(r'[A-Za-z][0-9.]+$')


def malformed(line: str) -> bool:
    """Klipper's rule for extended commands (ECHO, DUMP_TMC, SET_TMC_FIELD...): the
    arguments are shell-quoted KEY=VALUE words, '#' and ';' start a comment; a bare
    word or an unclosed quote is a 'Malformed command'. Traditional commands (a letter
    and a number, like G1 or M117) take free text."""
    command, _, raw = line.strip().partition(' ')
    if not command or command.startswith(';') or TRADITIONAL.match(command):
        return False
    lexer = shlex.shlex(raw, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = '#;'
    try:
        return any('=' not in word for word in lexer)
    except ValueError:
        return True


def send(sock, message: dict):
    sock.sendall(json.dumps(message).encode() + b'\x03')


def run_script(sock, request: dict, respond) -> 'list[str]':
    """Play a gcode/script request like Klipper: each line's console output goes out
    before the response, an ECHO prints its own line with the '// ' prefix, and a
    malformed line prints '!! ...' and aborts the rest of the script with an error
    response. `respond(line)` returns the console lines a command prints. Returns the
    lines that ran."""
    ran = []
    for line in request['params']['script'].split('\n'):
        if malformed(line):
            error = "Malformed command '%s'" % line
            send(sock, {'key': 'gcode_output', 'params': {'response': '!! ' + error}})
            send(sock, {'id': request['id'],
                        'error': {'error': 'WebRequestError', 'message': error}})
            return ran
        ran.append(line)
        output = ['// ' + line] if line.startswith('ECHO ') else respond(line)
        for text in output:
            send(sock, {'key': 'gcode_output', 'params': {'response': text}})
    send(sock, {'id': request['id'], 'result': {}})
    return ran
