import secrets,time,hmac

def scope_allowed(umo,sender,platform,private,allowlist):
    return bool(private and platform=="aiocqhttp" and str(sender).isdigit()
                and umo=="feiyu-onebot:FriendMessage:"+str(sender) and umo in allowlist)

class Confirmations:
    def __init__(self,clock=time.monotonic):self.items={};self.clock=clock
    def issue(self,scope,sender):
        code=str(secrets.randbelow(900000)+100000)
        self.items[(scope,str(sender))]=(code,self.clock()+90)
        return code
    def consume(self,scope,sender,code):
        key=(scope,str(sender));item=self.items.get(key)
        if not item:return False
        if self.clock()>item[1]:self.items.pop(key,None);return False
        if not hmac.compare_digest(item[0],str(code)):return False
        self.items.pop(key,None);return True
