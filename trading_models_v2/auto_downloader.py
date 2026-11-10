import os
import pandas as pd
from datetime import datetime, timedelta
import dukascopy_python
from dukascopy_python import instruments # <--- THE CRITICAL FIX

def download_native_python(start_date_str, end_date_str):
    print("🐍 IGNITING PURE PYTHON TICK FORGE...")
    
    start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
    end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
    
    output_dir = "./gft_quant_data"
    os.makedirs(output_dir, exist_ok=True)
    
    # 1. Dynamically hunt for the exact BTCUSD enum in the library....
    target_instrument = instruments.INSTRUMENT_VCCY_ETH_USD
    '''
    target_instrument = None
    
    for attr in dir(instruments):
        if "BTC" in attr and "USD" in attr:
            target_instrument = getattr(instruments, attr)
            print(f"✅ Target instrument found: {target_instrument}")
            break

    if target_instrument is None:
        print("❌ CRITICAL: Failed to locate the target instrument.")
        quit() '''
    print(f"✅ Target instrument found: {target_instrument}")
    print(f"🎯 Starting download for {target_instrument} from {start_date_str} to {end_date_str}...")

    current_start = start_date
    
    while current_start < end_date:
        # Step forward exactly 7 days to prevent RAM overload
        current_end = current_start + timedelta(days=7)
        if current_end > end_date:
            current_end = end_date
            
        str_start = current_start.strftime("%Y-%m-%d")
        str_end = current_end.strftime("%Y-%m-%d")
        filename = os.path.join(output_dir, f"ethusd_ticks_{str_start}_to_{str_end}.csv")
        
        print("\n" + "="*50)
        print(f"📥 FETCHING NATIVELY: {str_start} to {str_end}")
        print("="*50)
        
        try:
            # 2. The Native Python Fetch Command
            df = dukascopy_python.fetch(
                instrument=target_instrument,
                interval=dukascopy_python.INTERVAL_TICK,
                offer_side=dukascopy_python.OFFER_SIDE_BID, 
                start=current_start,
                end=current_end,
                max_retries=10, 
                debug=False
            )
            
            # 3. Save directly to CSV and clear memory
            if df is not None and not df.empty:
                df.index.name = 'timestamp'
                df['bidVolume'] = df['bidVolume'] * 1000000
                df['askVolume'] = df['askVolume'] * 1000000
                df.to_csv(filename, index=True) 
                print(f"✅ Successfully saved {len(df):,} ticks to {filename}")
            else:
                print(f"⚠️ Warning: No data returned for {str_start} to {str_end} (Market likely closed).")
                
        except Exception as e:
            print(f"❌ ERROR fetching {str_start}: {e}")
            print("Stopping automation so you can investigate.")
            break
            
        current_start = current_end

if __name__ == "__main__":
    # Your exact 2-year window
    START = "2022-01-01"
    END = "2026-07-04" 
    
    download_native_python(START, END) 