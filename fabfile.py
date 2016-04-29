import sshtunnel
import os
import requests
import json
import re
from datetime import datetime
import sys
import yaml
import glob
from novaclient import client
import pyfscache

from urlparse import urlparse

from pprint import pprint
from natsort import natsorted

from fabric.api import env, local, settings, hide, run, task, get, put
from fabric.network import disconnect_all
from fabric.context_managers import cd
from fabric.contrib.files import exists, sed
from StringIO import StringIO

YUM_HOST="10.57.34.179"
calling_user=env.user
env.user="vcidev"
env.password="password4Lusers!"
env.host_string=YUM_HOST
basedir=os.path.dirname(os.path.abspath(__file__))

glacier_utils_root=basedir
mfst_root=os.path.abspath(basedir + "/../mfst")
glacier_root=os.path.abspath(basedir + "/../glacier")

@task
def test_conn(mfstroot=mfst_root):
    mfstroot=os.path.abspath(mfstroot)
    run("hostname")
    run("echo glacier_root is " + glacier_root)
    run("echo mfstroot is " + mfstroot)
    run("echo mfst_rootis " + mfst_root)
    env.host_string="10.57.59.145"
    run("hostname")

RCDIR="/var/www/html/spvss-cloud-ci-yum-release/release-candidate"
SANDBOXDIR="/var/www/html/spvss-cloud-ci-yum-release/sandbox"

REPO_HOMES=[RCDIR,SANDBOXDIR]


def get_user_and_email():
    try:
        email=local("git config --global user.email",capture=True)
        name=local("git config --global user.name",capture=True)
    except:
        email,name=None,None
    return (name,email)

def get_latest_rc(base=None):
    """ Determine the most recently created/modified release_candidate repo """
    if base is None:
        with cd(RCDIR):
            latest=run("ls -1|sort | grep -v sandbox | tail -1".format(base=base))
            print "got latest " + latest
        return RCDIR,latest
    else:
        for option in REPO_HOMES:
            if exists("%s/%s" %(option,base)):
                return option,base
    raise RuntimeError("cannot find dir %s in %s" % (base,repr(REPO_HOMES)))
    
@task
def prep_new(suffix=calling_user,clean=False,base=None):
    """copy the latest release-candidate repo dir into sandbox with a specified suffix

    If the repo sandbox has already been "prepared" with this suffix
    it will do nothing (and return the directory)

    """
    parent_dir,latest=get_latest_rc(base)
    sourcedir = "%s/%s" % (parent_dir,latest)
    newdir = "%s/%s_%s" % (SANDBOXDIR,latest,suffix)
    with cd(SANDBOXDIR):
        if clean and exists(newdir):
            run ("rm -rf %s" % newdir)
        if not exists(newdir):
            run("cp -dR {sourcedir} {newdir}".format(**locals()))
            run("echo Created by {calling_user} at {time} UTC > {newdir}/CREATED_BY.txt".format(calling_user=calling_user,time=str(datetime.utcnow()),**locals()))
        else:
            print "{newdir} already exists".format(**locals())
    return newdir

@task
def list_comps(mfst=mfst_root,silent=False):
    """ list the components defined in the mfst that this script can auto-update """
    comps = [f.replace(".js","") for f in os.listdir("{mfst}/components".format(**locals())) if ".js" in f]
    if not silent:
        for f in comps:
            print f
    return comps

def get_files_from_dir(uri):
    """ Find the files/subdirs in the specified artifactory uri directory
    """
    return local("""curl -s {uri} | grep href= | fgrep -v ../ | cut -f 2 -d\\" """.format(uri=uri),capture=True).split("\n")
    

def get_rpms_from_dir(uri):
    """ Find the rpms in the specified uri directory """
    files=get_files_from_dir(uri)
    print "got files: " + repr(files)
    return [f for f in files if f.endswith(".rpm")]
    
def get_latest_comp_ver(comp,mfst=mfst_root,newver=None):
    """ Get info about the latest (or specified) version for the given component """
    
    comps=list_comps(mfst,True)
    if comp not in comps:
        print "error, cannot file component %s" % comp
        return
    jsfile="%s/components/%s.js" % (mfst,comp)
    with open(jsfile) as jsf:
        compdesc=json.load(jsf)
    uri = compdesc["source_uri"]
    print "uri is:" + uri
    if uri[-1] != "/":
        uri += "/"

    # get files in dir but only if they begin with a number (named
    # versions will be ignored and must be processed manually)
    vers = [f.replace("/","") for f in get_files_from_dir(uri) if re.match(r'^\d+\..*',f)]
    
    lines= natsorted(vers)

    if newver is not None:
        if newver not in vers:
            raise RuntimeError("error, requested version %s not found, avaliable versions are " % newver + repr(lines))
        else:
            print "forcing version " + newver + " instead of latest found " + lines[-1]
            latest = newver
    else:
        latest=lines[-1]
    print "got latest %s uri: %s " % (latest, uri+latest)
    rpmdir = uri + latest + "/Packages/"
    rpms= get_rpms_from_dir(rpmdir)
    print "got rpms: " + repr(rpms)
    return (latest, uri + latest , rpms,rpmdir )

def get_rpmbase(rpm):
    rpmbase= re.sub(r'(-|_v)\d+\.\d+.*',"",rpm)
    print "rpm= %s rpmbase = %s" % (rpm,rpmbase)
    return rpmbase
    


@task
def prep_with_latest_comps(comps,suffix=calling_user,mfst=mfst_root,base=None,clean=False,destination=None,glacierroot=glacier_root,flush_deployer_ips=False,deployer_ip=None):
    """Prepare a new sandbox repo on shared-yum-repo

    Example Usage:
       fab prep_with_latest_comps:ctap+ui-server^+schedule@8.5.0-54,suffix=jtest1p

    will take the latest "release-candidate", and

    1. copy it into the sandbox with the given suffix,

    2. update this repo's ctap and ui-server to the latest available
    in artifactory and will update schedule to the specific version.

    3. the "^" tag will indicate to increase the "installer_version"
    field of the ui_server component (regardless of whether the rpm
    version is increased or not)
    
    It assumes that the "mfst" repository is a sibling directory to
    the glacier repo this is run from, if not add an argument
    mfst=/path/to/mfst/root so it can locate the component metadata

    If the repo sandbox has already been "prepared" it will update that repo

    4. It will create a new manifest file

    5. if the "desitnation" flag is set it will update the tenant .sls
    file (on the tenant's deployer) to point to the new repository
    that it created.  It will also download the manifest to /tmp

    """

    if not os.path.exists(mfst):
        print """cannot find mfst directory {mfst};
please specify as mfst_root or clone it from
ssh://git@bitbucket-eng-rtp1.cisco.com:7999/vgwcis/mfst.git "next" to this
repository (e.g. as {mfst_root}).
e.g.:
        cd {basedir}
        git clone ssh://git@bitbucket-eng-rtp1.cisco.com:7999/vgwcis/mfst.git

        """.format(mfst_root=mfst_root,mfst=mfst,basedir=basedir)
        sys.exit(1)
    
    if destination is not None:
        #if destination in destination_deployer_ips:
            #destination_ip = destination_deployer_ips[destination]
        if deployer_ip is not None:
            destination_ip = get_deployer_ip(destination,glacierroot,flush_deployer_ips)
        else:
            destination_ip = deployer_ip

    newdir = prep_new(suffix,clean,base)
    newbase = os.path.basename(newdir)
    newver = None
    newversions_taken = {}
    # get the manifest
    fd = StringIO()
    get(newdir+"/*.manifest",fd)
    manifest=json.loads(fd.getvalue())
    #print("got manifest: ")
    #pprint(manifest)
    for comp in comps.split("+"):
        newver = None
        if "@" in comp:
            comp,newver=comp.split("@")
            print "forcing version %s for component %s" % (newver,comp)
        if "^" in comp:
            comp = comp.replace("^","")
            insver = int(manifest["components"][comp]["installer_version"]) + 1
            manifest["components"][comp]["installer_version"] = str(insver)
            if newver is None:
                # keep the current version when ^ is specified without a version
                newver = manifest["components"][comp]["version"]
            newversions_taken[comp + " (installer_version)"] = "%d --> %d " % (insver -1 ,insver)
            print("increasing installer_version for %s to %d " % (comp,insver))
        with cd("%s/%s" % ( newdir,comp)):
            oldver=run("/bin/ls | grep -v repodata").split("\n")
            if len(oldver) != 1:
                raise RuntimeError("couldn't uniqeuly identify old version in %s/%s" % (newdir,comp))
            oldver=oldver[0]
            latest,latesturi,rpms,rpmdir = get_latest_comp_ver(comp,mfst,newver)
            print "oldver = %s latest = %s" % (oldver,latest)
            if (oldver == latest):
                print "for comp %s no change in version: %s" % (comp,oldver)
                newversions_taken[comp] = latest + " (no change)"
                continue
            newversions_taken[comp] = oldver + " --> " + latest
            manifest["components"][comp]["version"] = latest
            #TODO figure out way to auto-update installer_version when needed
            run("mv %s %s" % (oldver,latest))
            with cd("%s/Packages" % latest):
                oldrpms = run("ls").split("\n")
                # loop twice in case there are shared prefixes -- we don't want to delete anything after we have downloaded it!
                for rpm in rpms:
                    if (exists(rpm)):
                        print "%s already present, not re-downloading" % rpm
                    else:
                        # if we have a new rpm, delete the old version, download the new
                        base = get_rpmbase(rpm)
                        run ("rm -f {base}*".format(base=base))
                for rpm in rpms:
                    if (exists(rpm)):
                        print "%s already present, not re-downloading" % rpm
                    else:
                        # if we have a new rpm, delete the old version, download the new
                        base = get_rpmbase(rpm)
                        run ("wget {rpmdir}/{rpm}".format(**locals()))
                newrpms = run("ls").split("\n")
                # sanity check in case we seem to have lost rpms
                if len(newrpms) < len(oldrpms):
                    raise RuntimeError("check for possible runtime error; number of rpms has gone down.  If all is OK run createrepo manually")
            run("rm -rf repodata")
            run("createrepo .")
            
    run("rm %s/*.manifest" % newdir)
    name,email=get_user_and_email()
    if (name is not None and name != ""):
        manifest["info"]["name"] = name
    if (email is not None and email != ""):
        manifest["info"]["email"] = email
    print "name=%s email=%s" % (manifest["info"]["name"],manifest["info"]["email"])
    manifest["release"] = newbase # XXX not sure if needed
    put(StringIO(json.dumps(manifest,sort_keys=True,indent=4, separators=(',', ': '))),"%s/%s.manifest" % (newdir , newbase))
    print "versions updated (old --> new) : "
    pprint(newversions_taken)
    manifest_url_dir="http://{YUM_HOST}/spvss-cloud-ci-yum-release/release-candidate/sandbox/{newbase}".format(newbase=newbase,YUM_HOST=YUM_HOST)
    manifest_url = "{manifest_url_dir}/{newbase}.manifest".format(newbase=newbase,manifest_url_dir=manifest_url_dir)

    if destination is not None:
        env.host_string=destination_ip
        dest_file="/srv/pillar/tenant/{destination}-tenant.sls".format(destination=destination)
        sed(dest_file,"repo_url:.*","repo_url: %s" % manifest_url_dir,use_sudo=True)
        put(StringIO(json.dumps(manifest,sort_keys=True,indent=4, separators=(',', ': '))),"%s/%s.manifest" % ("/tmp" , newbase))

            
            

    print "manifest_url= {manifest_url}".format(**locals())
    print "repo_url= {manifest_url_dir}".format(**locals())
        
tenant_subpath="/ronin/srv/pillar/tenant/"
def known_tenants(glacierroot=glacier_root):
    files=glob.glob(glacierroot + tenant_subpath + "*-tenant.sls")
    tenants = [os.path.basename(f).replace("-tenant.sls","") for f in files]
    return sorted(tenants)

def get_tenant_nova_client(tsls):
    #print tsls["OS_USERNAME"],tsls["OS_PASSWORD"], tsls["OS_TENANT_ID"],tsls["OS_AUTH_URL"]
    return client.Client("2.0",tsls["OS_USERNAME"],tsls["OS_PASSWORD"],
                         tsls["OS_TENANT_NAME"],tenant_id=tsls["OS_TENANT_ID"],auth_url=tsls["OS_AUTH_URL"],
                         region_name=tsls["OS_REGION_NAME"])



def get_tenant_sls(tenant,glacierroot):
    tenant_yaml=glacierroot + tenant_subpath + "%s-tenant.sls" % tenant
    if (not os.path.exists(glacierroot)):
        raise RuntimeError("expected glacier root " + glacierroot + """ doesn't exist.  Please either clone glacier into that directory (e.g. from ssh://git@bitbucket-eng-rtp1.cisco.com:7999/vgwcis/glacier.git or some fork thereof) or specifiy the path to your glacier root via the glacierroot argument
e.g.:
        cd {basedir}/..
        git clone  ssh://git@bitbucket-eng-rtp1.cisco.com:7999/vgwcis/glacier.git
        """.format(basedir=basedir))
    if (not os.path.exists(tenant_yaml)):
        print "can't find tenant %s under glacier_root %s" % (tenant,glacierroot)
        print "Known tenants: " + repr(known_tenants(glacierroot))
        raise RuntimeError("can't find tenant %s under glacier_root %s" % (tenant,glacierroot))
    tsls=yaml.load(file(tenant_yaml,"r"))
    return tsls
    
@task
def get_horizon_url(tenant,glacierroot=glacier_root):
    """ Print (and return) the horizon url for the specfied tenant """
    
    tsls = get_tenant_sls(tenant,glacierroot)
    host=urlparse(tsls["OS_AUTH_URL"]).hostname
    horizon_url= "http://%s/horizon" % (host)
    print horizon_url
    return horizon_url

CACHEDIR=basedir + "/deployer_ips.cache"
cache_ip = pyfscache.FSCache(CACHEDIR,days=7)

@cache_ip
def get_cached_deployer_ip(tenant,glacierroot):
    tsls=get_tenant_sls(tenant,glacierroot)
    nc=get_tenant_nova_client(tsls)
    #print repr(nc)
    instances=nc.servers.list()
    print "got deps: "
    for i in instances:
        if "deployer" in i.name.lower():
            print i.name 
    dep = [i.networks for i in instances if "deployer" in i.name.lower() and "mongo" not in i.name][0]
    print "got dep servers " + repr(dep)
    depip=dep[dep.keys()[0]][0]
    return depip

@task
def get_deployer_ip(tenant,glacierroot=glacier_root,clearcache=False):
    """ print (and return) the IP of the specified tenant's deployer
    (Note it uses some heuristics and can get confused if there are multiple
    deployers for a given tenant).

    It also caches the IPs so run with clearcache=True if the deployer has been rebuilt
    """
    if clearcache:
        cache_ip.purge()
    ip=get_cached_deployer_ip(tenant,glacier_root)
    print ip
    return ip

@task
def list_tenants(glacierroot=glacier_root):
    """ list all known tenants in the specified/default glacier """
    tenants = known_tenants(glacier_root)
    for t in tenants:
        print t

        
@task
def osvars(tenant, glacier_root=glacier_root):
    """return the environment variables you would need to set to enable nova/ heat CLI tools for the given tenant

    """
    tsls = get_tenant_sls(tenant,glacier_root)
    for k in sorted(tsls.keys()):
        if k.startswith("OS"):
            print "export %s=%s" % (k, tsls[k])

@task
def latest_comps(comps,mfst=mfst_root,base=None,glacierroot=glacier_root):
    """ List the latest versions avilable for the specified components """
    list=[]
    for comp in comps.split("+"):
        newver = None
        latest,latesturi,rpms,rpmdir = get_latest_comp_ver(comp,mfst)
        list += [[comp,latest]]

    for pair in list:
        print repr(pair)

@task
def delete_stacks(tenant,glacierroot=glacier_root):
    """
    Placeholder for routine to delete stacks prior to a full deployment.  Not Yet Implemented
    """
    # delete everything but the adminsg and ih-services
    print "not for running, just for documentation..."
    return
    run("heat stack-delete -y $(heat stack-list | awk '{print $4}' | egrep . | grep -v stack_name | egrep -v 'ih-services|adminsg')")
    run("nova delete $(nova list | awk '{print $4}' | egrep . | grep -v Name | egrep -iv 'deployer|consul')")
    # for g in $(nova secgroup-list | awk '{print $4}' | egrep . | grep -v Name | egrep -v 'default|consul|ec_agent|bissli'  ); do
    # nova secgroup-delete $g
    # done

    run("heat stack-delete -y $(heat stack-list | awk '{print $4}' | egrep . | grep -v stack_name | egrep  'ih-services|adminsg' )    ")

