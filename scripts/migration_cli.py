"""Private child worker; the public JSON contract stays in controller.py."""
import contextlib
import json
import sys
sys.dont_write_bytecode=True
import controller as c
import migration
from cryptography.exceptions import InvalidTag
import zipfile

if __name__=='__main__':
    args=c.parser().parse_args();root=c.owned_root(args.root.resolve())
    password=c.read_stdin()['password']
    try:
        with contextlib.redirect_stdout(sys.stderr):
            if args.command=='migration-export':result=migration.export(root,c.state(root),args.destination.resolve(),password)
            else:result=migration.import_package(root,args.package.resolve(),password,args.confirm_source_offline,args.cache,args.offline)
    except (InvalidTag,zipfile.BadZipFile):
        print(json.dumps({'kind':'integrity','user_error':'迁移密码错误或迁移包损坏；没有恢复任何用户数据，请确认密码及文件完整性。'}));raise SystemExit(23)
    except (ValueError,FileExistsError,c.Fault) as exc:
        print(json.dumps({'kind':getattr(exc,'kind','integrity'),'user_error':str(exc)}));raise SystemExit(23)
    print(json.dumps(result))
