"""How many parts the `python` workflow splits each Python's suite into (#391): the one number
`select.py` and `coverage_floor.py` share. Standard library only, so the coverage
job needs nothing but `coverage` installed (#429)."""

PARTS = 10
