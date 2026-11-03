import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from p2c.policy import ScopedPolicy,PendingActions
class Permissions(unittest.TestCase):
 def setUp(self):self.p=ScopedPolicy('100',{'900':'mention','901':'smart'},{'900':{'300'}},{'600'},{'900':{'700'}},global_admins={'200'})
 def test_roles(self):
  for actor,friend,group,level in [('100',False,'',3),('200',False,'',2.5),('300',False,'900',2),('300',False,'901',0),('400',True,'',1),('500',False,'',0),('600',True,'900',-1),('700',True,'900',-1)]:self.assertEqual(self.p.level(actor,friend,group),level)
 def test_global_inheritance(self):
  self.assertGreater(self.p.level('100'),self.p.level('200'));self.assertGreater(self.p.level('200'),self.p.level('300',False,'900'))
  for group in self.p.groups:
   self.assertTrue(self.p.authorize('200','group',private=False,group=group))
   self.assertTrue(self.p.authorize('200','admin',private=False,group=group,target='400'))
  self.assertTrue(self.p.authorize('200','whitelist',private=True))
 def test_owner_only_grants(self):
  for actor in ['200','300','400','500','600']:
   self.assertFalse(self.p.authorize(actor,'global_admin',private=True,target='500'))
  self.assertTrue(self.p.authorize('100','global_admin',private=True,target='500'))
  self.assertFalse(self.p.authorize('100','global_admin',private=True,target='100'))
 def test_block_dominates_every_identity(self):
  for actor in ['100','200','300','400']:
   self.p.blocked.add(actor)
   for op in ['global_admin','admin','whitelist','blacklist','group','memory']:
    self.assertFalse(self.p.authorize(actor,op,private=False,group='900',target='500'))
 def test_scope_and_protected_targets(self):
  self.assertFalse(self.p.authorize('300','group',private=False,group='901'))
  self.assertFalse(self.p.authorize('300','whitelist',private=True))
  self.assertFalse(self.p.authorize('200','blacklist',private=True,target='100'))
  self.assertFalse(self.p.authorize('300','blacklist',private=False,group='900',target='200'))
 def test_confirm_binding(self):
  now=[0];p=PendingActions(lambda:now[0]);code=p.issue('100','private','global_admin',('add','200'))
  self.assertIsNone(p.consume('200','private',code));self.assertIsNone(p.consume('100','group:900',code))
  self.assertEqual(p.consume('100','private',code),('global_admin',('add','200')));self.assertIsNone(p.consume('100','private',code))
  code=p.issue('100','private','global_admin',('add','200'));now[0]=91;self.assertIsNone(p.consume('100','private',code))
if __name__=='__main__':unittest.main(verbosity=2)
