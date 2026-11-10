"""Pre-source independent signed-ledger oracle, no SQLite/product imports."""
import hashlib,json

def fold(events,initial=None):
    state={} if initial is None else {k:dict(v) for k,v in initial.items()}
    prefixes=[]
    for event in events:
        for item,delta in event['deltas'].items():
            cell=state.setdefault(item,{'authorized':0,'reserved':0,'received':0})
            for key in cell:cell[key]+=delta.get(key,0)
            assert 0<=cell['reserved']+cell['received']<=cell['authorized']<=10**12
            assert cell['reserved']>=0 and cell['received']>=0
        prefixes.append({item:{**cell,'available':cell['authorized']-cell['reserved']-cell['received']} for item,cell in sorted(state.items())})
    return prefixes

def corpus():
    count=0;h=hashlib.sha256()
    for authorized in range(6):
        for received in range(authorized+1):
            for reserved in range(authorized-received+1):
                available=authorized-reserved-received
                for q in range(1,available+1):
                    for accepted in range(q+1):
                        initial={'i':{'authorized':authorized,'reserved':reserved,'received':received}}
                        out=fold([{'deltas':{'i':{'reserved':q}}},{'deltas':{'i':{'reserved':-q,'received':accepted}}}],initial)
                        assert out[-1]['i']=={'authorized':authorized,'reserved':reserved,'received':received+accepted,'available':available-accepted}
                        if accepted:
                            restored=fold([{'deltas':{'i':{'received':-accepted}}}],{'i':{k:v for k,v in out[-1]['i'].items() if k!='available'}})[-1]
                            assert restored['i']=={**initial['i'],'available':available}
                        h.update(json.dumps([initial,q,accepted,out],sort_keys=True,separators=(',',':')).encode()+b'\n');count+=1
    amendments=[]
    for authorized in range(6):
        for received in range(authorized+1):
            for reserved in range(authorized-received+1):
                for limit in range(6):amendments.append({'received':received,'reserved':reserved,'new_limit':limit,'allowed':limit>=received+reserved})
    return {'accepted_delta_sequences':count,'sha256':h.hexdigest(),'amendment_admission_cases':len(amendments),'amendment_digest':hashlib.sha256(json.dumps(amendments,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
