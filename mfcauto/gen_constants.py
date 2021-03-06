"""
Utility script for pulling down the latest constant values from MyFreeCams.com
"""
import re
from urllib.request import urlopen
URL = "http://www.myfreecams.com/_js/mfccore.js"
# Maybe it's wrong to merge in the w. stuff?  Is that all just for the UI?
CONSTANT_RE = re.compile(r'(\s|;?|,)(FCS|w)\.([A-Z0-9]+)_([A-Z0-9_]+)\s+?=\s+?([0-9]+);')
CONSTANT_MAP = dict()

HEADER = """\"\"\"
Various constants and enums used by MFC. Most of these values
can be seen here: http://www.myfreecams.com/_js/mfccore.js

DO NOT EDIT. This file is generated by ./gen_constants.py.
\"\"\"
# pylint: disable=missing-docstring, invalid-name, trailing-newlines
from enum import IntEnum
MAGIC = -2027771214

class STATE(IntEnum):
    \"\"\"STATE is essentially the same as FCVIDEO but has friendly
    names for better log messages and code readability\"\"\"
    FreeChat = 0            # TX_IDLE
    #TX_RESET = 1           # Unused?
    Away = 2                # TX_AWAY
    #TX_CONFIRMING = 11     # Unused?
    Private = 12            # TX_PVT
    GroupShow = 13          # TX_GRP
    ClubShow = 14           # Unused? It is now used for club show
    #TX_KILLMODEL = 15      # Unused?
    #C2C_ON = 20            # Unused?
    #C2C_OFF = 21           # Unused?
    Online = 90             # RX_IDLE
    ViewingPrivate = 91     # RX_PVT, Members enter this state when viewing privates
    #RX_VOY = 92            # Unused?
    #RX_GRP = 93            # Unused?
    #NULL = 126             # Unused?
    Offline = 127           # OFFLINE
"""

#Add our own constants...
CONSTANT_MAP.setdefault("FCTYPE", dict())["CLIENT_TAGSLOADED"] = -6
CONSTANT_MAP.setdefault("FCTYPE", dict())["CLIENT_DISCONNECTED"] = -5
CONSTANT_MAP.setdefault("FCTYPE", dict())["CLIENT_MODELSLOADED"] = -4
CONSTANT_MAP.setdefault("FCTYPE", dict())["CLIENT_CONNECTED"] = -3
CONSTANT_MAP.setdefault("FCTYPE", dict())["ANY"] = -2
CONSTANT_MAP.setdefault("FCTYPE", dict())["UNKNOWN"] = -1

with urlopen(URL) as data:
    SCRIPT_TEXT = data.read().decode('utf-8')

    FOUND_CONSTANTS = CONSTANT_RE.findall(SCRIPT_TEXT)
    for (prefix1, prefix2, fctype, subtype, num) in FOUND_CONSTANTS:
        CONSTANT_MAP.setdefault(fctype, dict())[subtype] = num

    with open("constants.py", "w") as f:
        f.write(HEADER)
        for fctype in sorted(CONSTANT_MAP):
            f.write("\nclass {}(IntEnum):\n".format(fctype))
            for subtype, value in sorted(CONSTANT_MAP[fctype].items(), key=lambda x: int(x[1])):
                f.write("    {} = {}\n".format(subtype.replace("60DAY", "SIXTYDAY"), value))
            #f.write("\n")
        f.write("\n")

print("Done")
