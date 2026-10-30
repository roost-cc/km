#!/bin/sh
# A stand-in for a remote transport such as ssh to a host: logs the shell command
# it is given (its one argument) to $FAKE_TRANSPORT_LOG, then runs it locally.
printf 'transport: %s\n' "$1" >> "$FAKE_TRANSPORT_LOG"
exec sh -c "$1"
