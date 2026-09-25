# Copyright (C) 2026 xsm909
#
# This file is part of xverb-plugins.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""What `ssh` runs when it wants a password.

`ssh` asks for a secret on its terminal, and a plugin has none; with
`SSH_ASKPASS` set it runs this instead and reads the answer from stdout. The
prompt is the first argument.

**Only a password or a passphrase is answered.** The secret comes from the
environment of the `ssh` that ran this, which the plugin set for that one
process — never from the command line, where any other user's `ps` would see
it. A question that is not about a secret — "are you sure you want to
continue connecting?" — is answered no: this program cannot see the
fingerprint it would be agreeing to, and the connection is started with
`accept-new`, so the only question left is about a key that has *changed*.
"""

import os
import sys


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    lowered = prompt.lower()
    if "yes/no" in lowered or "(yes/no" in lowered:
        sys.stdout.write("no\n")
        return 0
    secret = os.environ.get("XVERB_SFTP_SECRET", "")
    if not secret:
        # Failing is the answer: ssh gives up on this method at once instead
        # of waiting for a secret nobody is going to type.
        return 1
    sys.stdout.write(secret + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
