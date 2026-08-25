"""
Persisted state. The initial position entered on both sides, with number
of shares, cost, and date.

When expanded to working with orders, should be the exact values of first
order submitted.

There should be three tracked values: (1) the amount we are willing to expose
to this strategy, (2) the max position size we can open when accounting for 
margin, and (3) the amount we actually spent to open the position, which 
must be lower than (2). We get (3) from this file for now.

Set up now to handle multiple pairs by keying to leveraged ticker (and match
peak equity to this format).

Should be in format:
    {pair_key_1 (str):
        {lev_ticker (str): 
            {initial_price: float,
            initial_shares: float,
            created_on: date
            },
         base_ticker (str): 
            {initial_price: float,
            initial_shares: float,
            created_on: data
            }
        },    
     pair_key_2 (str):
         {lev_ticker (str): 
             {initial_price: float,
             initial_shares: float,
             created_on: date
             },
          base_ticker (str): 
             {initial_price: float,
             initial_shares: float,
             created_on: data
             }
         },
    }
"""

import datetime
import json
import logging
import os
import config

log = logging.getLogger(__name__)

# Local-dev default. Deployment MUST override MONITOR_STATE_PATH to a path
# OUTSIDE the repo tree: a git pull or re-clone that wipes this file resets the
# peak and silently disables the drawdown stop, with nothing in the logs to say
# the safety net is gone. See docs/AUTOMATION.md.
# TODO set a path for this to match monitor state
DEFAULT_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "state", "initial.json"
)


def state_path():
    """Resolve the state file path from env, falling back to the local default."""
    return os.environ.get("MONITOR_STATE_PATH") or DEFAULT_STATE_PATH


def load_all(path):
    """Return persisted initial_position, or None if no state file exists yet.

    The goal is to be calculating unrealize PnL off of this number. If this
    file is missing values or missing completely, we should raise to fix this.
    """
    if not os.path.exists(path):
        log.info("no state file at %s -- first run", path)
        return None

    with open(path) as f:
        raw = f.read()

    try:
        data = json.loads(raw)
        
    except Exception as e:
        raise SystemExit(
            f"There is no initial position file or it is malformed."
            f"({e})"
            f"Inspect or create the file manually to proceed, or create initial"
            f"order with bot."
        )
        
    tickers = data.keys()
    
    if len(tickers) == 0:
        raise SystemExit(
            "There are no tickers stored in file. Create a position first."    
        )

    log.info("stored initial position", data, path)
    return data


def load_ticker(path, pair_key):
    """
    Get initial data for one ticker only, using pair key. 
    """
    all_data = load_all(path)
    
    try:
        pair = config.PAIRS[pair_key]
    except Exception as e:
        raise SystemExit(
            f"{pair_key} is not in general list of pairs."
            f"Exception: {e}"
        )
    
    try:
        pair_data = all_data[pair_key]
    except Exception as e:
        raise SystemExit(
            f"{pair_key} is not in list of initial positions. Make sure a"
            "position has been created for this ticker."
            f"Exception: {e}"
        )
        
    return pair_data


def save(path, lev_ticker, lev_price, lev_shares, base_ticker, base_price, base_shares):
    """Write initial positions. Creates the parent directory on first write.
    
    Should be in format:
        pair_key (str):
            {lev_ticker (str): 
                {initial_price: float,
                initial_shares: float,
                created_on: date
                },
             base_ticker (str): 
                {initial_price: float,
                initial_shares: float,
                created_on: data
                }
            }
    """
    if os.path.exists(path):
        with open(path, "r") as f:
            data = json.load(f)
    else:
        data = {}
        
    if lev_ticker in data.keys():
        raise ValueError(f"Pair key '{lev_ticker}' already exists in {path}")
        
    data[lev_ticker] = {
        lev_ticker: {
            "initial_price": lev_price,
            "initial_shares": lev_shares,
            "created_on": datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            },
        base_ticker: {
            "initial_price": base_price,
            "initial_shares": base_shares,
            "created_on": datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            }
        }

    with open(path, "w") as f:
        json.dump(data, f, indent=2)