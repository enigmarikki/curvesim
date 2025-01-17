"""
Test the entire sim pipeline, starting with canned data.

Technically these are not end-to-end tests since we don't pull the data,
so besides the network code there is a lack of coverage around the
`pool_data` and `price_data` packages.
"""
import argparse
import os
import pickle

import pandas as pd

import curvesim


def main(generate=False, ncpu=None):
    """
    Run the vol-limited arb pipeline across the given pools, pulling
    pool and market data using the specified timestamp.

    Use `generate` to create and pickle the test data for comparison
    with the output of future code changes.

    Setting `ncpu` to 1 is useful for debugging and profiling.
    """
    data_dir = os.path.join("test", "data")

    pools = [
        # 3CRV
        {
            "address": "0xbebc44782c7db0a1a60cb6fe97d0b483032ff1c7",
            "end_timestamp": 1638316800,
        },
        # aCRV
        {
            "address": "0xdebf20617708857ebe4f679508e7b7863a8a8eee",
            "end_timestamp": 1622505600,
        },
        # frax3CRV"
        {
            "address": "0xd632f22692fac7611d2aa1c0d552930d43caed3b",
            "end_timestamp": 1643673600,
        },
        # ousd3CRV
        {
            "address": "0x87650d7bbfc3a9f10587d7778206671719d9910d",
            "end_timestamp": 1646265600,
        },
        # rai3CRV
        # {
        #     "address": "0x618788357d0ebd8a37e763adab3bc575d54c2c7d",
        #     "end_timestamp": 1654041600,
        # },
        # triCRV
        {
            "address": "0x4ebdf703948ddcea3b11f675b4d1fba9d2414a14",
            "end_timestamp": 1692215156,
            "env": "staging",
        },
    ]

    test_functions = {
        "summary": summary,
        "per_run": per_run,
        "per_trade": per_trade,
    }

    for pool in pools:
        address = pool["address"]
        end_ts = pool["end_timestamp"]
        vol_mult = pool.get("vol_mult", None)
        env = pool.get("env", "prod")

        results = curvesim.autosim(
            pool=address,
            chain="mainnet",
            end=end_ts,
            test=True,
            vol_mult=vol_mult,
            ncpu=ncpu,
            env=env,
        )

        sim_data = {
            "per_run": results.data_per_run,
            "per_trade": results.data(),
            "summary": results.summary(),
        }

        for key in test_functions:
            f_name = os.path.join(data_dir, f"{address}-results_{key}.pickle")

            if generate:
                with open(f_name, "wb") as f:
                    pickle.dump(sim_data[key], f)
            else:
                stored_data = pd.read_pickle(f_name)
                test_functions[key](sim_data[key], stored_data)


def per_run(sim, stored):
    print("Testing per-run data...")

    # Compare metric columns
    compare_metrics(sim.columns, stored.columns)
    sim = sim[stored.columns]

    # Compare runs
    sim_runs = list(sim.index)
    stored_runs = list(stored.index)
    assert (
        sim_runs == stored_runs
    ), "Simulation runs don't match between stored and tested data."

    # Test exact equality
    are_equal = sim == stored

    # Feedback
    if not are_equal.all(axis=None):
        print("Per-run data: Equality Test")
        print(are_equal)
        raise AssertionError("Equality test failed.")

    print("Equality test passed.")


def per_trade(sim, stored, threshold=0.9):
    print("Testing per-trade data...")

    # Compare metric columns
    compare_metrics(sim.columns, stored.columns)
    sim = sim[stored.columns]

    # Compare runs
    sim_runs = list(sim["run"].unique())
    stored_runs = list(stored["run"].unique())
    assert (
        sim_runs == stored_runs
    ), "Simulation runs don't match between stored and tested data."

    # Compute R-squared
    R2 = []
    for run in stored_runs:
        _sim = sim[sim["run"] == run].set_index("timestamp").drop("run", axis=1)
        _stored = (
            stored[stored["run"] == run].set_index("timestamp").drop("run", axis=1)
        )
        assert (
            _sim.shape == _stored.shape
        ), f"Per trade data, run {run}: data shape mismatch"

        R2.append(compute_R2(_sim.resample("1D").mean(), _stored.resample("1D").mean()))

    R2 = pd.DataFrame(R2).T

    # Feedback
    if not (R2 >= threshold).all(axis=None):
        print(R2)
        raise AssertionError(f"R-squared test failed. (Threshold: {threshold})")

    print(R2)
    print(f"R-squared test passed. (Threshold: {threshold}).")


def summary(sim, stored, threshold=0.99):
    print("Testing summary data...")

    # Compare metric columns
    compare_metrics(sim.columns, stored.columns)
    sim = sim[stored.columns]

    # Compare runs
    sim_runs = list(sim.index)
    stored_runs = list(stored.index)
    assert (
        sim_runs == stored_runs
    ), "Simulation runs don't match between stored and tested data."

    # Compute R-squared
    R2 = compute_R2(sim, stored)

    # Feedback
    if not (R2 >= threshold).all(axis=None):
        print(R2)
        raise AssertionError(f"R-squared test failed. (Threshold: {threshold})")

    print(R2)
    print(f"R-squared test passed. (Threshold: {threshold}).")


def compare_metrics(test, reference):
    extra, missing = compare_elements(test, reference)
    assert not missing, f"Metrics missing from simulation results: {missing}"

    if extra:
        print("WARNING: extra untested metrics in simulation results:", extra)


def compare_elements(test, reference):
    extra = [el for el in test if el not in reference]
    missing = [el for el in reference if el not in test]
    return extra, missing


def compute_R2(sim, stored):
    MSE = ((sim - stored) ** 2).sum()
    total_variance = stored.var(ddof=0)
    return 1 - MSE / total_variance


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="Vol-limited CI Test",
        description="Test end-to-end by running the vol-limited arb pipeline across multiple pool types",
    )
    parser.add_argument(
        "-g",
        "--generate",
        action="store_true",
        help="Generate pickled test data",
    )
    parser.add_argument(
        "-n",
        "--ncpu",
        type=int,
        help="Number of cores to use; use 1 for debugging/profiling",
    )
    args = parser.parse_args()
    main(args.generate, args.ncpu)
