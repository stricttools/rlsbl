# Campaign install restore command

Companion record to the campaign plan in this directory (phase 0.0). Not a
todo: this file records how to restore the machine-wide rlsbl install after
the campaign's fleet sweep completes (phase 10.5), and is deleted with the
plan when the campaign closes.

During the campaign the machine-wide rlsbl is pinned to the pre-campaign
patch release (a normal, non-editable install of rlsbl 0.117.2). The
editable install is restored ONLY after the fleet workspace sweep has run
(sweep first, restore last — see the campaign lifecycle in the plan).

The exact restore command:

    uv tool install --force -e /home/m/Projects/rlsbl

The fleet workspace list is deliberately NOT recorded here or anywhere in
this repository: the sweep discovers workspaces at run time or reads a
local-only file outside version control.
