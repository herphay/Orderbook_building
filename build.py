import databento as db

def main():
    store = db.DBNStore.from_file('XNAS_MBO/xnas-itch-20261009.mbo.dbn.zst')
    df = store.to_df()

    # We will drop columns we do not need
    # KEEP:
    # ts_event -> matching engine dt + sequence to account for order of orders
    # symbol
    # action
    # side
    # price
    # size
    # order_id
    # flags -> only when flag = 128 does orderbook update. Apply all orders from last 128
    #          til the current flag = 128 (e.g. 1 big order hitting multiple sitting orders)

    # DROP:
    # ts_recv (index) -> databento receive time; ts_in_delta -> matching eng sending dt
    # instrument_id -> use symbol
    # publisher_id (all 2); rtype (all 160: market-by-order record); channel_id (all 0)
    
    return df


if main == '__main__':
    main()