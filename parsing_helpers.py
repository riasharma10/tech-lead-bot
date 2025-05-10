from typing import AsyncIterator, Optional, List, Dict, Any, Tuple
import re

def split_into_hunks(patch: str) -> List[Tuple[int, str]]:
    """
    Parse a unified diff patch into discrete hunks, returning list of
    (new_start_line, hunk_text). Only hunks with both removals and additions
    are returned.
    """
    hunks: List[Tuple[int, str]] = []
    lines = patch.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        header = lines[i]
        if header.startswith('@@'):
            # Parse new file starting line from hunk header of form "@@ -a,b +c,d @@"
            m = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@', header)
            print("m: ", m)
            if m:
                new_start = int(m.group(1))
                # Collect hunk body
                hunk_lines = [header]
                i += 1
                while i < len(lines) and not lines[i].startswith('@@'):
                    hunk_lines.append(lines[i])
                    i += 1
                # Only include if both deletions and additions exist
                # if any(l.startswith('-') for l in hunk_lines) and any(l.startswith('+') for l in hunk_lines):
                hunks.append((new_start, ''.join(hunk_lines)))
                continue
        i += 1
    return hunks
   
def extract_added_line_numbers(hunk_text: str, new_start: int) -> List[int]:
    """
    Given a unified diff hunk (including header), return the list of line numbers
    in the new file corresponding to each added ('+') line.
    """
    lines = hunk_text.splitlines()
    # First line is the header, skip it
    current_new = new_start
    added_lines: List[int] = []
    for line in lines[1:]:
        if line.startswith(' '):
            # Context line: advances both old and new
            current_new += 1
        elif line.startswith('-'):
            # Removal: advances old only
            continue
        elif line.startswith('+'):
            # Addition: record current new line then advance
            added_lines.append(current_new)
            current_new += 1
        else:
            # Other (e.g. \ No newline at end), ignore
            continue
    return added_lines