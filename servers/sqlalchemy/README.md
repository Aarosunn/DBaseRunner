# LittleX SQLAlchemy

Drop in replacement for [littleX_fullstack](https://github.com/cse584-W26/littleX/tree/main/littleX_FULLSTACK)

Works for the same APIs, but backed with native Python/Flask/SQLAlchemy, either in-memory data store or postgres.

### Usage

Clone and install depencies, either with `uv sync` or `pip install -r requirements.txt`. The only real dependencies are flask/sqlalchemy and the postgres integration.

You can run the app like any flask app by just running `flask --app src run`. By default, it'll use a memory-backed sqlite database. If you specifiy a postgres url as an environment variable (e.g. `export DATABASE_URL=postgresql+psycopg://localhost:5432/littlex`), it'll use that instead.

### Comparison

While this version isn't incredibly fast or optimized, it doesn't suffer from the exponential time blowup that the Jac version does. Below are two tables that measure import times from the [benchmark import_data script](https://github.com/cse584-W26/littleX-benchmarks/blob/main/import_data.py) at different amounts of users. These were run locally using the dev server for each on a M1 Max Macbook Pro

At a glance, the dataset consists of
- 15,000 tweets
- 5,001 users
- 23,162 follows
- 7,543,197 total likes

#### User Creation
| Users | SQLAlchemy Memory | SQLAlchemy Postgres | Jaseci | 
| --- | --- | --- | --- |
| **1000**  | 4s  | 20s | 95s |
| **5000**  | 24s | 53s | 45 min | 

#### Data Import
| Users | SQLAlchemy Memory | SQLAlchemy Postgres | Jaseci | 
| --- | --- | --- | --- |
| **1000**  | 3s  | 15s | 16s |
| **5000**  | 17s | 81s | 6 min | 