import json, urllib.request, datetime, time, pathlib, os, http.client, urllib.error, sys

OUT=pathlib.Path(__file__).parent
RPC=os.environ.get('ALCHEMY_RPC_URL', '').strip()
POOL='0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640'
DATE=datetime.date.fromisoformat(sys.argv[1] if len(sys.argv)>1 else '2026-10-01')
DATE_TEXT=DATE.isoformat()
START=int(datetime.datetime.combine(DATE,datetime.time(),tzinfo=datetime.timezone.utc).timestamp())
END=START+86400

def rpc(method,params):
    body=json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params}).encode()
    for attempt in range(5):
        try:
            req=urllib.request.Request(RPC,body,{'Content-Type':'application/json','Connection':'close','Accept-Encoding':'identity'})
            with urllib.request.urlopen(req,timeout=45) as resp: data=json.load(resp)
            if 'error' in data: raise RuntimeError(data['error'])
            return data['result']
        except (http.client.IncompleteRead, TimeoutError, ConnectionError, urllib.error.URLError) as e:
            if attempt==4: raise
            delay=2**attempt
            print(f'{method}: connection interrupted; retry {attempt+1}/4 in {delay}s',flush=True)
            time.sleep(delay)

def block(n): return rpc('eth_getBlockByNumber',[hex(n),False])
def lower_bound(ts,hi):
    lo=0
    while lo<hi:
        mid=(lo+hi)//2
        if int(block(mid)['timestamp'],16)<ts: lo=mid+1
        else: hi=mid
    return lo

def main():
    status={'date_utc':DATE_TEXT,'pool':POOL,'rpc_provider':'Alchemy Ethereum Mainnet','complete':False,'chunks':0,'logs':0,'bytes':0}
    try:
        if not RPC.startswith('https://eth-mainnet.g.alchemy.com/v2/'):
            raise ValueError('ALCHEMY_RPC_URL is missing or is not an Ethereum Mainnet HTTPS endpoint')
        head=int(rpc('eth_blockNumber',[]),16)
        first=lower_bound(START,head); end=lower_bound(END,head)
        status.update(first_block=first,last_block=end-1)
        print(f'block range {first}-{end-1}',flush=True)
        data_file=OUT/f'events_{DATE_TEXT}.ndjson'
        status_file=OUT/f'fetch_status_{DATE_TEXT}.json'
        a=first
        mode='w'
        if status_file.exists() and data_file.exists():
            previous=json.loads(status_file.read_text(encoding='utf-8'))
            if previous.get('complete'):
                print('Dataset already complete; no changes made',flush=True)
                return
            if previous.get('pool')==POOL and previous.get('first_block')==first and previous.get('last_block')==end-1 and previous.get('last_completed_block') is not None:
                a=previous['last_completed_block']+1
                status=previous
                status.pop('stop_reason',None)
                mode='a'
                print(f'resuming at block {a}; preserving {status["logs"]} logs',flush=True)
            else:
                raise RuntimeError('Existing partial dataset cannot be resumed safely; no data overwritten')
        start_chunks=status['chunks']
        with data_file.open(mode,encoding='utf-8') as f:
            span=20
            while a<end:
                b=min(a+span-1,end-1)
                try:
                    logs=rpc('eth_getLogs',[{'fromBlock':hex(a),'toBlock':hex(b),'address':POOL}])
                except urllib.error.HTTPError as e:
                    if e.code in (400,413,429) and span>1:
                        span=max(1,span//2)
                        print(f'HTTP {e.code} for blocks {a}-{b}; reducing batch to {span} blocks',flush=True)
                        time.sleep(2)
                        continue
                    raise
                for log in logs:
                    line=json.dumps(log,separators=(',',':'))+'\n'; f.write(line); status['bytes']+=len(line.encode())
                status['chunks']+=1; status['logs']+=len(logs)
                status['last_completed_block']=b
                f.flush()
                print(f'{a}-{b}: {len(logs)} logs; total {status["logs"]}; {status["bytes"]/1048576:.2f} MiB',flush=True)
                status_file.write_text(json.dumps(status,indent=2),encoding='utf-8')
                if status['logs']>30000 or status['bytes']>20*1048576:
                    status['stop_reason']='size cap'; break
                if status['chunks']-start_chunks>=1000:
                    status['stop_reason']='request cap (1000 batches in this run)'; break
                a=b+1
                time.sleep(.2)
            else: status['complete']=True
    except Exception as e: status['stop_reason']=str(e); print('STOP:',e,flush=True)
    (OUT/f'fetch_status_{DATE_TEXT}.json').write_text(json.dumps(status,indent=2),encoding='utf-8')
    print(json.dumps(status),flush=True)

if __name__=='__main__': main()
