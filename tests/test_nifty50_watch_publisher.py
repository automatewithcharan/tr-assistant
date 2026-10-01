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
