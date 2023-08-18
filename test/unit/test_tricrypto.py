"""
Unit tests for CurveCryptoPool for n = 3

Tests are against the tricrypto-ng contract.
"""
import os
from time import time
from itertools import permutations

import boa
from hypothesis import HealthCheck, assume, given, settings, Phase
from hypothesis import strategies as st

from curvesim.pool import CurveCryptoPool
from curvesim.pool.cryptoswap.calcs import get_p, get_y, newton_D
from curvesim.pool.cryptoswap.calcs.tricrypto_ng import (
    MAX_A,
    MAX_GAMMA,
    MIN_A,
    MIN_GAMMA,
    PRECISION,
    _newton_y,
    wad_exp,
)


def pack_A_gamma(A, gamma):
    """
    Need this to set A and gamma in the smart contract since they
    are stored in packed format.
    """
    A_gamma = A << 128
    A_gamma = A_gamma | gamma
    return A_gamma


def pack_3_uint64s(nums):
    return (nums[0] << 128) | (nums[1] << 64) | nums[0]


def pack_prices(prices):
    return (prices[1] << 128) | prices[0]


def get_math(tricrypto):
    get_math_start = time()

    _base_dir = os.path.dirname(__file__)
    filepath = os.path.join(_base_dir, "../fixtures/curve/tricrypto_math.vy")
    _math = tricrypto.MATH()
    MATH = boa.load_partial(filepath).at(_math)

    get_math_end = time()
    test_point = f"get_math (boa.load_partial): {get_math_end - get_math_start} s"
    print(test_point)
    return MATH


def initialize_pool(vyper_tricrypto):
    """
    Initialize python-based pool from the state variables of the
    vyper-based implementation.
    """
    initialize_pool_start = time()

    A = vyper_tricrypto.A()
    gamma = vyper_tricrypto.gamma()
    n_coins = 3
    precisions = vyper_tricrypto.precisions()
    mid_fee = vyper_tricrypto.mid_fee()
    out_fee = vyper_tricrypto.out_fee()
    fee_gamma = vyper_tricrypto.fee_gamma()

    admin_fee = vyper_tricrypto.ADMIN_FEE()

    allowed_extra_profit = vyper_tricrypto.allowed_extra_profit()
    adjustment_step = vyper_tricrypto.adjustment_step()
    ma_half_time = vyper_tricrypto.ma_time()

    price_scale = [vyper_tricrypto.price_scale(i) for i in range(n_coins - 1)]
    price_oracle = [vyper_tricrypto.price_oracle(i) for i in range(n_coins - 1)]
    last_prices = [vyper_tricrypto.last_prices(i) for i in range(n_coins - 1)]
    last_prices_timestamp = vyper_tricrypto.last_prices_timestamp()
    balances = [vyper_tricrypto.balances(i) for i in range(n_coins)]
    # Use the cached `D`. See warning for `virtual_price` below
    D = vyper_tricrypto.D()
    lp_total_supply = vyper_tricrypto.totalSupply()
    xcp_profit = vyper_tricrypto.xcp_profit()
    xcp_profit_a = vyper_tricrypto.xcp_profit_a()

    pool = CurveCryptoPool(
        A=A,
        gamma=gamma,
        n=n_coins,
        precisions=precisions,
        mid_fee=mid_fee,
        out_fee=out_fee,
        allowed_extra_profit=allowed_extra_profit,
        fee_gamma=fee_gamma,
        adjustment_step=adjustment_step,
        admin_fee=admin_fee,
        ma_half_time=ma_half_time,
        price_scale=price_scale,
        price_oracle=price_oracle,
        last_prices=last_prices,
        last_prices_timestamp=last_prices_timestamp,
        balances=balances,
        D=D,
        tokens=lp_total_supply,
        xcp_profit=xcp_profit,
        xcp_profit_a=xcp_profit_a,
    )

    assert pool.A == vyper_tricrypto.A()
    assert pool.gamma == vyper_tricrypto.gamma()
    assert pool.balances == balances
    assert pool.price_scale == price_scale
    assert pool._price_oracle == price_oracle  # pylint: disable=protected-access
    assert pool.last_prices == last_prices
    assert pool.last_prices_timestamp == last_prices_timestamp
    assert pool.D == vyper_tricrypto.D()
    assert pool.tokens == lp_total_supply
    assert pool.xcp_profit == xcp_profit
    assert pool.xcp_profit_a == xcp_profit_a

    # WARNING: both `virtual_price` and `D` are cached values
    # so depending on the test, may not be updated to be
    # consistent with the rest of the pool state.
    #
    # Allowing this simplifies testing since we can avoid
    # coupling tests of basic functionality with the tests
    # for the complex newton calculations.
    #
    # We think it makes sense the initialized pool should
    # at least match the vyper pool (inconsistencies and all).
    # When full consistency is required, the `update_cached_values`
    # helper function should be utilized before calling
    # `initialize_pool`.
    virtual_price = vyper_tricrypto.virtual_price()
    pool.virtual_price = virtual_price

    initialize_pool_end = time()
    test_point = f"initialize_pool: {initialize_pool_end - initialize_pool_start} s"
    print(test_point)

    return pool


def get_real_balances(virtual_balances, precisions, price_scale):
    """
    Convert from units of D to native token units using the
    given price scale.
    """
    assert len(virtual_balances) == 3
    balances = [x // p for x, p in zip(virtual_balances, precisions)]
    balances[1] = balances[1] * PRECISION // price_scale[0]
    balances[2] = balances[2] * PRECISION // price_scale[1]
    return balances


def update_cached_values(vyper_tricrypto):
    """
    Useful test helper after we manipulate the pool state.

    Calculates `D` and `virtual_price` from pool state and caches
    them in the appropriate storage.
    """
    update_cached_start = time()

    A = vyper_tricrypto.A()
    gamma = vyper_tricrypto.gamma()
    xp = vyper_tricrypto.eval("self.xp()")
    xp = list(xp)  # boa doesn't like its own tuple wrapper

    get_math_start = time()
    MATH = get_math(vyper_tricrypto)
    get_math_end = time()
    get_math_time = get_math_end - get_math_start
    D = MATH.newton_D(A, gamma, xp)  # pylint: disable=no-member

    vyper_tricrypto.eval(f"self.D={D}")
    total_supply = vyper_tricrypto.totalSupply()
    vyper_tricrypto.eval(
        f"self.virtual_price=10**18 * self.get_xcp({D}) / {total_supply}"
    )

    update_cached_end = time()
    test_point = f"update_cached_values - get_math: {(update_cached_end - update_cached_start) - get_math_time} s"
    print(test_point)


def override_initialization(vyper_tricrypto, A, gamma, balances):
    """
    Helper that overrides the vyper_tricrypto fixture's
    attributes. End result emulates a balanced, newly
    created pool with 0 profit or loss.
    """
    boa_init_start = time()

    # USDT, WBTC, WETH
    decimals = [6, 8, 18]
    precisions = [10 ** (18 - d) for d in decimals]

    xp = balances

    packed_A_gamma = pack_A_gamma(A, gamma)
    vyper_tricrypto.eval(f"self.initial_A_gamma = {packed_A_gamma}")
    vyper_tricrypto.eval(f"self.future_A_gamma = {packed_A_gamma}")

    vyper_tricrypto.eval(f"self.balances = {xp}")

    # assume newly created pool with balanced reserves
    price_scale = [
        (xp[0] * precisions[0] * PRECISION) // (xp[i] * precisions[i])
        for i in range(1, len(xp))
    ]
    packed_prices = pack_prices(price_scale)
    vyper_tricrypto.eval(f"self.price_scale_packed = {packed_prices}")
    vyper_tricrypto.eval(f"self.price_oracle_packed = {packed_prices}")
    vyper_tricrypto.eval(f"self.last_prices_packed = {packed_prices}")

    normalized = [xp[0] * precisions[0]] + [
        xp[i] * precisions[i] * price_scale[i - 1] // PRECISION
        for i in range(1, len(xp))
    ]

    get_math_start = time()
    MATH = get_math(vyper_tricrypto)
    get_math_end = time()
    get_math_time = get_math_end - get_math_start

    D = MATH.newton_D(A, gamma, normalized)
    vyper_tricrypto.eval(f"self.D = {D}")

    # assume newly created pool with 0 profit or loss
    vyper_tricrypto.eval("self.xcp_profit = 10**18")
    vyper_tricrypto.eval("self.xcp_profit_a = 10**18")
    xcp = vyper_tricrypto.internal.get_xcp(D)
    vyper_tricrypto.eval(f"self.totalSupply = {xcp}")
    vyper_tricrypto.eval("self.virtual_price = 10**18")

    # WE MIGHT NOT NEED TO CALL update_cached_values() AT ALL HERE
    update_cached_start = time()
    update_cached_values(vyper_tricrypto)
    update_cached_end = time()
    update_cached_time = update_cached_end - update_cached_start

    boa_init_end = time()
    test_point = f"override_initialization - get_math - update_cached_values: \
        {(boa_init_end - boa_init_start) - get_math_time - update_cached_time} s"
    print(test_point)

    return vyper_tricrypto


def override_initialization_python(vyper_tricrypto, A, gamma, balances):
    """
    Helper that overrides the attributes of a CurveCryptoPool
    initialized from the vyper_tricrypto fixture. 
    End result emulates a balanced, newly
    created pool with 0 profit or loss.
    """
    python_init_start = time()

    initialize_pool_start = time()
    pool = initialize_pool(vyper_tricrypto)
    initialize_pool_end = time()
    initialize_pool_time = initialize_pool_end - initialize_pool_start

    # USDT, WBTC, WETH
    decimals = [6, 8, 18]
    precisions = [10 ** (18 - d) for d in decimals]

    xp = balances

    pool.A = A
    pool.gamma = gamma

    pool.balances = xp

    # assume newly created pool with balanced reserves
    price_scale = [
        (xp[0] * precisions[0] * PRECISION) // (xp[i] * precisions[i])
        for i in range(1, len(xp))
    ]
    pool.price_scale = price_scale
    pool._price_oracle = price_scale
    pool.last_prices = price_scale

    normalized = [xp[0] * precisions[0]] + [
        xp[i] * precisions[i] * price_scale[i - 1] // PRECISION
        for i in range(1, len(xp))
    ]
    D = newton_D(A, gamma, normalized)
    pool.D = D

    # assume newly created pool with 0 profit or loss
    pool.xcp_profit = 10**18
    pool.xcp_profit_a = 10**18
    xcp = pool._get_xcp(D)
    pool.tokens = xcp
    pool.virtual_price = 10**18

    python_init_end = time()
    test_point = f"override_initialization_python - initialize_pool: \
        {(python_init_end - python_init_start) - initialize_pool_time} s"
    print(test_point)

    return pool


D_UNIT = 10**18
positive_balance = st.integers(min_value=10**5 * D_UNIT, max_value=10**11 * D_UNIT)
amplification_coefficient = st.integers(min_value=MIN_A, max_value=MAX_A)
gamma_coefficient = st.integers(min_value=MIN_GAMMA, max_value=MAX_GAMMA)
price = st.integers(min_value=10**12, max_value=10**25)


@given(
    st.integers(min_value=1, max_value=300),
    st.integers(min_value=0, max_value=2),
    st.integers(min_value=0, max_value=2),
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=5,
    deadline=None,
)
def test_exchange(vyper_tricrypto, dx_perc, i, j):
    """Test `exchange` against vyper implementation."""
    assume(i != j)

    pool = initialize_pool(vyper_tricrypto)
    dx = pool.balances[i] * dx_perc // 100

    expected_dy = vyper_tricrypto.exchange(i, j, dx, 0)
    dy, _ = pool.exchange(i, j, dx)
    assert dy == expected_dy

    expected_balances = [vyper_tricrypto.balances(i) for i in range(3)]
    assert pool.balances == expected_balances


_num_iter = 10


@given(
    st.lists(
        st.integers(min_value=1, max_value=10000),
        min_size=_num_iter,
        max_size=_num_iter,
    ),
    st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=2),
            st.integers(min_value=0, max_value=2),
        ).filter(lambda x: x[0] != x[1]),
        min_size=_num_iter,
        max_size=_num_iter,
    ),
    st.lists(
        st.integers(min_value=0, max_value=86400),
        min_size=_num_iter,
        max_size=_num_iter,
    ),
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=5,
    deadline=None,
)
def test_multiple_exchange_with_repeg(
    vyper_tricrypto, dx_perc_list, indices_list, time_delta_list
):
    """Test `exchange` against vyper implementation."""

    pool = initialize_pool(vyper_tricrypto)

    for indices, dx_perc, time_delta in zip(
        indices_list, dx_perc_list, time_delta_list
    ):
        vm_timestamp = boa.env.vm.state.timestamp
        pool._block_timestamp = vm_timestamp

        i, j = indices
        dx = pool.balances[i] * dx_perc // 10000  # dx_perc in bps

        expected_dy = vyper_tricrypto.exchange(i, j, dx, 0)
        dy, _ = pool.exchange(i, j, dx)
        assert dy == expected_dy

        expected_balances = [vyper_tricrypto.balances(i) for i in range(3)]
        assert pool.balances == expected_balances

        assert pool.last_prices == [vyper_tricrypto.last_prices(i) for i in range(2)]
        assert pool.last_prices_timestamp == vyper_tricrypto.last_prices_timestamp()

        expected_price_oracle = [vyper_tricrypto.price_oracle(i) for i in range(2)]
        expected_price_scale = [vyper_tricrypto.price_scale(i) for i in range(2)]
        assert pool.price_oracle() == expected_price_oracle
        assert pool.price_scale == expected_price_scale

        boa.env.time_travel(time_delta)


@given(
    amplification_coefficient,
    gamma_coefficient,
    positive_balance,
    positive_balance,
    positive_balance,
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=5,
    deadline=None,
)
def test_newton_D(vyper_tricrypto, A, gamma, x0, x1, x2):
    """Test D calculation against vyper implementation."""

    xp = [x0, x1, x2]
    assume(0.02 < xp[0] / xp[1] < 50)
    assume(0.02 < xp[1] / xp[2] < 50)
    assume(0.02 < xp[0] / xp[2] < 50)

    MATH = get_math(vyper_tricrypto)
    # pylint: disable=no-member
    expected_D = MATH.newton_D(A, gamma, xp)
    D = newton_D(A, gamma, xp)

    assert D == expected_D


@given(
    amplification_coefficient,
    gamma_coefficient,
    positive_balance,
    positive_balance,
    positive_balance,
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=2,
    deadline=None,
)
def test_get_p(vyper_tricrypto, A, gamma, x0, x1, x2):
    """Test `get_p` calculation against vyper implementation."""

    xp = [x0, x1, x2]
    assume(0.02 < xp[0] / xp[1] < 50)
    assume(0.02 < xp[1] / xp[2] < 50)
    assume(0.02 < xp[0] / xp[2] < 50)

    MATH = get_math(vyper_tricrypto)
    # pylint: disable=no-member
    D = MATH.newton_D(A, gamma, xp)

    A_gamma = [A, gamma]
    expected_p = MATH.get_p(xp, D, A_gamma)
    p = get_p(xp, D, A, gamma)

    assert p == expected_p


@given(
    amplification_coefficient,
    gamma_coefficient,
    positive_balance,
    positive_balance,
    positive_balance,
    st.tuples(
        st.integers(min_value=0, max_value=2),
        st.integers(min_value=0, max_value=2),
    ).filter(lambda x: x[0] != x[1]),
    st.integers(min_value=1, max_value=10000),
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=5,
    deadline=None,
)
def test_pure_get_y(vyper_tricrypto, A, gamma, x0, x1, x2, pair, dx_perc):
    """Test `get_y` calculation against vyper implementation."""
    i, j = pair

    xp = [x0, x1, x2]
    assume(0.02 < xp[0] / xp[1] < 50)
    assume(0.02 < xp[1] / xp[2] < 50)
    assume(0.02 < xp[0] / xp[2] < 50)

    MATH = get_math(vyper_tricrypto)
    # pylint: disable=no-member
    D = MATH.newton_D(A, gamma, xp)

    xp[i] += xp[i] * dx_perc // 10000

    expected_y_out = MATH.get_y(A, gamma, xp, D, j)
    y_out = get_y(A, gamma, xp, D, j)

    assert y_out[0] == expected_y_out[0]
    assert y_out[1] == expected_y_out[1]


def test_pool_get_y(vyper_tricrypto):
    """
    Test `CurveCryptoPool.get_y`.

    Note the pure version of `get_y` is already tested
    thoroughly in its own test against the vyper.

    This test is a sanity check to make sure we pass values in correctly
    to the pure `get_y` implementation.
    """
    pool = initialize_pool(vyper_tricrypto)

    xp = pool._xp()
    A = pool.A
    gamma = pool.gamma
    D = newton_D(A, gamma, xp)

    i = 0
    j = 1

    # `get_y` will set i-th balance to `x`
    x = xp[i] * 102 // 100
    y = pool.get_y(i, j, x, xp)

    xp[i] = x
    expected_y, _ = get_y(A, gamma, xp, D, j)

    assert y == expected_y


@given(st.integers(min_value=-42139678854452767551, max_value=135305999368893231588))
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=2,
    deadline=None,
)
def test_wad_exp(vyper_tricrypto, x):
    """Test the snekmate wad exp calc"""
    MATH = get_math(vyper_tricrypto)
    # pylint: disable=no-member
    expected_result = MATH.wad_exp(x)
    result = wad_exp(x)
    assert result == expected_result


@given(
    amplification_coefficient,
    gamma_coefficient,
    positive_balance,
    positive_balance,
    positive_balance,
    st.tuples(
        st.integers(min_value=0, max_value=2),
        st.integers(min_value=0, max_value=2),
    ).filter(lambda x: x[0] != x[1]),
    st.integers(min_value=1, max_value=10000),
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=1,
    deadline=None,
)
def test__newton_y(vyper_tricrypto, A, gamma, x0, x1, x2, pair, dx_perc):
    """Test D calculation against vyper implementation."""
    i, j = pair

    xp = [x0, x1, x2]
    assume(0.02 < xp[0] / xp[1] < 50)
    assume(0.02 < xp[1] / xp[2] < 50)
    assume(0.02 < xp[0] / xp[2] < 50)

    MATH = get_math(vyper_tricrypto)
    # pylint: disable=no-member
    D = MATH.newton_D(A, gamma, xp)

    xp[i] += xp[i] * dx_perc // 10000

    expected_y = MATH.eval(f"self._newton_y({A}, {gamma}, {xp}, {D}, {j})")
    y = _newton_y(A, gamma, xp, D, j)

    assert y == expected_y


def test_dydxfee(vyper_tricrypto):
    """Test spot price formula against execution price for small trades."""
    pool = initialize_pool(vyper_tricrypto)

    # USDT, WBTC, WETH
    decimals = [6, 8, 18]
    precisions = [10 ** (18 - d) for d in decimals]

    # print("WBTC price:", pool.price_scale[0] / 10**18)
    # print("WETH price:", pool.price_scale[1] / 10**18)

    dxs = [
        10**6,
        10**4,
        10**15,
    ]

    for pair in permutations([0, 1, 2], 2):
        i, j = pair

        dydx = pool.dydxfee(i, j)
        dx = dxs[i]
        dy = vyper_tricrypto.exchange(i, j, dx, 0)
        pool.exchange(i, j, dx, 0)  # update state to match vyper pool

        dx *= precisions[i]
        dy *= precisions[j]
        assert abs(dydx - dy / dx) / (dy / dx) < 1e-4


@given(
    amplification_coefficient,
    gamma_coefficient,
    positive_balance,
    st.floats(min_value=0.021, max_value=49.999),
    st.floats(min_value=0.021, max_value=49.999),
    st.tuples(
        st.integers(min_value=0, max_value=2), st.integers(min_value=0, max_value=2)
    ).filter(lambda x: x[0] != x[1]),
    st.integers(min_value=1, max_value=100),
)
@settings(
    suppress_health_check=[
        HealthCheck.function_scoped_fixture,
        HealthCheck.filter_too_much,
    ],
    max_examples=25,
    deadline=None,
    phases=(
        Phase.reuse,
        Phase.generate,
    ),  # no Phase.explicit, Phase.target, Phase.shrink - waste a ton of time
)
def test_dydxfee_boa(vyper_tricrypto, A, gamma, x0, x1, x2, pair, dx_perc):
    """
    Test spot price formula against execution price for 1-100 bps
    volume trades. % Lower bound on error is ~ the number of bps traded.

    Overrides vyper_tricrypto's init from Boa for timing purposes.
    """
    x1 = int(x1 * D_UNIT * x0 // D_UNIT)
    x2 = int(x2 * D_UNIT * x0 // D_UNIT)

    i, j = pair

    # USDT, WBTC, WETH
    decimals = [6, 8, 18]
    xp = [x * 10 ** decimals[i] // D_UNIT for i, x in enumerate([x0, x1, x2])]

    vyper_tricrypto = override_initialization(vyper_tricrypto, A, gamma, xp)
    pool = initialize_pool(vyper_tricrypto)

    # dxs = [
    #     10**6,
    #     10**4,
    #     10**15,
    # ]
    # dx = dxs[i]

    # D_UNIT precision to mitigate floating point arithmetic error
    dydx = pool.dydxfee(i, j) * D_UNIT
    dx = xp[i] * dx_perc // 10000  # basis points increase
    dy = pool.exchange(i, j, dx, 0)[0]

    dx *= pool.precisions[i]
    dy *= pool.precisions[j]
    discretized = dy * D_UNIT // dx
    assert (
        abs(dydx - discretized) * D_UNIT // discretized
        < (dx_perc + 5) * D_UNIT // 10000
    )


@given(
    amplification_coefficient,
    gamma_coefficient,
    positive_balance,
    st.floats(min_value=0.021, max_value=49.999),
    st.floats(min_value=0.021, max_value=49.999),
    st.tuples(
        st.integers(min_value=0, max_value=2), st.integers(min_value=0, max_value=2)
    ).filter(lambda x: x[0] != x[1]),
    st.integers(min_value=1, max_value=100),
)
@settings(
    suppress_health_check=[
        HealthCheck.function_scoped_fixture,
        HealthCheck.filter_too_much,
    ],
    max_examples=25,
    deadline=None,
    phases=(
        Phase.reuse,
        Phase.generate,
    ),  # no Phase.explicit, Phase.target, Phase.shrink - waste a ton of time
)
def test_dydxfee_python(vyper_tricrypto, A, gamma, x0, x1, x2, pair, dx_perc):
    """
    Test spot price formula against execution price for 1-100 bps
    volume trades. % Lower bound on error is ~ the number of bps traded.

    Overrides vyper_tricrypto's init from Python for timing purposes.
    """
    x1 = int(x1 * D_UNIT * x0 // D_UNIT)
    x2 = int(x2 * D_UNIT * x0 // D_UNIT)

    i, j = pair

    # USDT, WBTC, WETH
    decimals = [6, 8, 18]
    xp = [x * 10 ** decimals[i] // D_UNIT for i, x in enumerate([x0, x1, x2])]

    pool = override_initialization_python(vyper_tricrypto, A, gamma, xp)

    # dxs = [
    #     10**6,
    #     10**4,
    #     10**15,
    # ]
    # dx = dxs[i]

    # D_UNIT precision to mitigate floating point arithmetic error
    dydx = pool.dydxfee(i, j) * D_UNIT
    dx = xp[i] * dx_perc // 10000  # basis points increase
    dy = pool.exchange(i, j, dx, 0)[0]

    dx *= pool.precisions[i]
    dy *= pool.precisions[j]
    discretized = dy * D_UNIT // dx
    assert (
        abs(dydx - discretized) * D_UNIT // discretized
        < (dx_perc + 5) * D_UNIT // 10000
    )

# things to time:
# boa load of vyper fixture
# overriding vyper fixture attributes
# initialize_pool(vyper_tricrypto)

# override_initialization_python(vyper_tricrypto)


# Plan for more thorough testing of dydxfee:
# other params:
# num. of times to do trades?

# use our dy solver's solution as the reference point for error
# in future, differentiate polynomial interpolated bonding curve
# (using a set of points on the curve, e.g. Chebyshev nodes)
# Newton interpolation or Barycentric form of Langrage interpolation
# at every point used for interpolation, assert abs. error of spot_price(point) < maximum error
# maximum error is based on trade size (or perhaps constant)
