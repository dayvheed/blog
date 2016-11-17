import json
import boto3
import os
import fileinput
import re
import base64
import mimetypes
import wget
import uuid
import string
import zipfile
import markdown
import shutil
from cStringIO import StringIO
import nbformat
from nbconvert import HTMLExporter
import hashlib
import datetime
import rpy2.robjects as robjects

def unzip(source_filename, dest_dir):
    zf = zipfile.ZipFile(source_filename)
    zf.extractall(dest)

def download(repo, branch, dest):
    src = repo + '/archive/' + branch +'.zip'
    if not(os.path.isdir(dest)):
        os.mkdir(dest)
    r1 = wget.download(src)
    unzip(r1, dest)
    os.remove(r1)
    filename = repo[repo.rfind('/')+1:] + '-' + branch 
    return filename

def get_src_dict(repo_root, parent, parsers):
    d = {}
    suffixes = parsers.keys()
    rootdir = repo_root.rstrip(os.sep)
    start = rootdir.rfind(os.sep) + 1
    for path, dirs, files in os.walk(rootdir):
        for file in files:
            i = file.rfind('.')
            if i != -1:
                ext = file[i:]
                if ext in suffixes:
                    d[path + '/' + file] = True
    return d

def get_content_dict(table):
    response = table.scan()
    items = response['Items']
    d = {}
    for item in items:
        uri = item['uri']
        d[uri] = item
    return d

def new_content_render_plan(repo_root, bucket, src_dict, content_dict, parsers, ignore):
    plan = []
    s = len(repo_root)
    srcs = src_dict.keys()
    oldHash = None
    for src in srcs:
        uri = src[s:]
        uri = bucket + uri
        if not(uri in ignore):
            isNew = True
            if content_dict.has_key(uri):
                isNew = False
                oldHash = content_dict[uri]['c_hash']
            ext = uri[uri.rfind('.'):]
            parser = parsers[ext]
            plan.append({"absfile": src, "uri": uri, "parser": parser, "isNew": isNew, "oldHash": oldHash})
    return plan

def get_title(absfilename, contents):
    lcontents = contents.lower()
    i = -1
    c = 1
    while i==-1 and c < 6:
        otag = '<h' + str(c) + '>'
        ctag = '</h' + str(c) + '>'
        i = lcontents.find(otag)
        j = lcontents.find(ctag)
        c += 1
    if i >= 0:
        return contents[i+len(otag):j]
    i = absfilename.rfind('/')
    j = absfilename.rfind('.')
    fname = absfilename[i+1:j]
    fname = fname.replace('-', ' ').replace('_', ' ')
    fname = fname.title()
    return fname

def get_pretty_name(absfilename, title):
    i = absfilename.rfind('/')
    if absfilename[i+1:].lower() == 'readme.htm':
        return '/' + absfilename[0:i+1]
    pn = title.lower().replace(' ', '-')
    valid_chars = "-_.() %s%s" % (string.ascii_letters, string.digits)
    pn2 = ''.join(c for c in pn if c in valid_chars)
    i = absfilename.rfind('/')
    return '/' + absfilename[0:i] + '/' + pn2

def get_desc(contents):
    lcontents = contents.lower()
    i = lcontents.find('<p>')
    if i != -1:
        j = lcontents.find('</p>', i)
        desc = contents[i+3:j-4]
        desc = re.sub('<[^<]+?>', '', desc)
        if desc != '':
            return desc
    return 'desc'

def execute_plan(plan, s3, bucket, table, env):
    summary = []
    now = datetime.datetime.now()
    n = now.strftime('%Y-%m-%d')
    tomorrow = now + datetime.timedelta(days=1)
    publish_date = tomorrow.strftime('%Y-%m-%d')
    for item in plan:
    	print(item['uri'])
        parser = item['parser']
        absfile = item['absfile']
        uri = item['uri']
        f = open(absfile, 'r')
        c = f.read()
        f.close()
        m = hashlib.md5()
        m.update(c)
        hash = m.hexdigest()
        render = True
        if not(item['isNew']):
            if item['oldHash'] == hash:
                render = False
            else:
                render = True
        if render:
            contents = parser(absfile)
            author = 'Kyle'
            title = get_title(absfile, contents)
            desc = get_desc(contents)
            if type(contents) == str:
            	contents = unicode(contents, 'utf-8')
            fake_handle = StringIO(contents.encode('utf-8'))
            a = len(bucket)+1
            i = uri.rfind('.')
            ext = uri[i:]
            s3key = uri[a:i] + '.htm'
            if s3key[0]=='/':
                s3key = s3key[1:]
            print('Deploying to: ' + bucket + '/' + s3key)
            prettyname = get_pretty_name(s3key, title)
            res = s3.Bucket(bucket).put_object(Key=s3key, Body=fake_handle)
            ritem = {
                        'uri': uri,
                        'ext': ext,
                        'last_rendered': n,
                        'c_hash': hash,
                        'date_discovered': n,
                        'env': env,
                        'author': author,
                        'desc': desc,
                        'prettyname': prettyname,
                        'publish_date': publish_date,
                        'rendered': s3key,
                        'title': title
                    }
            if item['isNew']:
                response = table.put_item(
                    Item=ritem
                )
            else:
                response = table.update_item(
                    Key={
                        'uri': uri
                    },
                    UpdateExpression="set last_rendered = :n, c_hash=:h",
                    ExpressionAttributeValues={
                        ':n': n,
                        ':h': hash
                    },
                    ReturnValues="UPDATED_NEW"
                )
            summary.append({"uri": uri})
    return summary

def send_summary(ses, summary, branch, bucket, recipients, efrom):
    response = ses.send_email(
        Source='kyle@dataskeptic.com',
        Destination={'ToAddresses': recipients},
        Message={
            'Subject': {
                'Data': 'Deploying ' + branch + ' to ' + bucket
            },
            'Body': {
                'Text': {
                    'Data': json.dumps(summary)
                }
            }
        },
        ReplyToAddresses=efrom
    )

def clean_up(dest):
    shutil.rmtree(dest)

def md(absfile):
    f = open(absfile, 'r')
    c = f.read()
    f.close()
    if type(c) == str:
    	c = c.replace('\x97', '-')
        c = unicode(c, 'utf-8')
    html = markdown.markdown(c)
    return html

def replacement(match):
    fn = match.groups()[0]
    if os.path.isfile(fn):
        return 'src="data:%s;base64,%s"' % (mimetypes.guess_type(fn)[0], base64.b64encode(open(fn, 'rb').read()))
    return match.group()

def html_inline(s):
    fi = fileinput.FileInput(openhook=fileinput.hook_encoded("utf8"))
    return re.sub(r'src="(.*?)"', replacement, s).encode('utf-8')

def knitr(absfile):
    i = absfile.rindex('/')
    j = absfile.find('.', i)
    s = absfile[0:j]
    figpath = s + '_img/'
    r = robjects.r("""
        knitr::opts_chunk$set(echo=FALSE, fig.path='{}')
        library('knitr')
        knit('{}')
        """.format(figpath, absfile))
    fname = r[0]
    f = open(fname, 'r')
    c = f.read()
    f.close()
    if type(c) == str:
        c = unicode(c, 'utf-8')
    c2 = html_inline(c)
    os.remove(fname)
    # TODO: remove directory also
    return c2

def nbconvert(absfile):
    f = open(absfile, 'r')
    c = f.read()
    f.close()
    nb = nbformat.reads(c, as_version=4)
    html_exporter = HTMLExporter()
    html_exporter.template_file = 'basic'
    (body, resources) = html_exporter.from_notebook_node(nb)
    return body

if __name__ == "__main__":
    repo = 'https://github.com/data-skeptic/blog'
    tblName = 'blog'
    emails = ['kylepolich@gmail.com']
    ignore = ['/README.md']
    #
    parsers = {
        '.md': md,
        '.Rhtml': knitr,
        '.ipynb': nbconvert
    }
    #
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(tblName)
    s3 = boto3.resource('s3')
    ses = boto3.client('ses')
    #
    environments = [
        {'branch': 'dev', 'bucket': 'dev.dataskeptic.com'}
        ,{'branch': 'master', 'bucket': 'dataskeptic.com'}
    ]
    #
    for env in environments:
        branch = env['branch']
        bucket = env['bucket']
        print("Running for " + branch)
        dest = '/tmp/' + str(uuid.uuid1()) + '/'
        filename = download(repo, branch, dest)
        repo_root = dest + filename
        # TODO: Check that no router paths match blog folders, /blog approx match goes to /blog/ml/2016/blah
        src_dict = get_src_dict(repo_root, filename, parsers)
        content_dict = get_content_dict(table)
        plan = new_content_render_plan(repo_root, bucket, src_dict, content_dict, parsers, ignore)
        summary = execute_plan(plan, s3, bucket, table, branch)
        if len(summary) > 0:
            send_summary(ses, summary, branch, bucket, emails, ['kyle@dataskeptic.com'])
        clean_up(dest)




