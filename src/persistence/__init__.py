# Public excerpt: the database layer (src/persistence/db.py) is omitted from this repository, so,
# unlike the private package, this __init__ does not re-export `db`. Every other module in this
# package is the real implementation; modules that need the database layer will not import.
