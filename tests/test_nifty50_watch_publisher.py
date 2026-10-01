from types import SimpleNamespace
from pkscreener.classes.Nifty50WatchPublisher import Nifty50WatchPublisher

class Transport:
    def __init__(self): self.messages=[]
    def post(self,message): self.messages.append(message); return SimpleNamespace(delivered=True)

def test_nifty_watch_is_default_off_and_dedupes(tmp_path):
    event={"event_id":"one","symbol":"ABC","event":"first_nifty50_top5_at"}
    assert Nifty50WatchPublisher(tmp_path,None).process([event])["status"] == "notification_disabled"
    transport=Transport(); watch=Nifty50WatchPublisher(tmp_path,transport,True)
    assert watch.process([event])["status"] == "delivered"
    assert watch.process([event])["status"] == "up_to_date"
    assert len(transport.messages)==1 and "WATCH ONLY" in transport.messages[0] and "BUY" in transport.messages[0]

def test_failed_delivery_survives_restart_and_retries(tmp_path):
    event={"event_id":"retry","symbol":"ABC","event":"first_nifty50_top10_at"}
    class Fail:
        def post(self,_): return SimpleNamespace(delivered=False,retry_after_seconds=0)
    assert Nifty50WatchPublisher(tmp_path,Fail(),True,lambda:1).process([event])["status"] == "delivery_failed"
    transport=Transport()
    assert Nifty50WatchPublisher(tmp_path,transport,True,lambda:2).process([])["status"] == "delivered"
    assert len(transport.messages)==1

def test_partial_success_does_not_resend_after_restart(tmp_path):
    events=[{"event_id":"a","symbol":"A","event":"first_nifty50_top5_at"},{"event_id":"b","symbol":"B","event":"first_nifty50_top5_at"}]
    class Partial:
        def __init__(self): self.n=0
        def post(self,_): self.n+=1; return SimpleNamespace(delivered=self.n==1,retry_after_seconds=0)
    first=Partial(); assert Nifty50WatchPublisher(tmp_path,first,True,lambda:1).process(events)["status"] == "delivery_failed"
    transport=Transport(); Nifty50WatchPublisher(tmp_path,transport,True,lambda:2).process([])
    assert len(transport.messages)==1 and "**B**" in transport.messages[0]
