#!/usr/bin/env python2

from __future__ import print_function

import argparse
from bz2 import BZ2File
from collections import Counter, defaultdict
import cPickle
from pymongo import MongoClient
import rpcz
from time import time

from extract import unwiki
import index_server_pb2 as index_pb
import index_server_rpcz as index_rpcz
from nlp import tokenise, normalise
import parse_wiki
from utils import grouper


parser = argparse.ArgumentParser(description='Populate index databases.')
parser.add_argument('dumpfile', help='dump-file path')
parser.add_argument('-m', '--mongocred', default='mongo.cred', help='path to MongoDB credentials', metavar='FILE')
parser.add_argument('-r', '--round', type=int, default=50, help='number of articles to process during one round', metavar='NUMBER')
args = parser.parse_args()


##
# Initialising index-store

ISERVER_ADDRESS = 'tcp://localhost:5555'
STORE_NAME = 'idontcare'

app = rpcz.Application()
iserver = index_rpcz.IndexBuilderService_Stub(
            app.create_rpc_channel(ISERVER_ADDRESS))
store = index_pb.StoreParameters()
store.location = STORE_NAME
store.overwrite = True
iserver.createStore(store, deadline_ms=1)


##
# Initialising MongoDB

with open(args.mongocred, 'rt') as f:
    MONGO_HOST = f.readline().strip()
    MONGO_DB   = f.readline().strip()
    MONGO_USER = f.readline().strip()
    MONGO_PASS = f.readline().strip()
MONGO_ADDRESS = 'mongodb://{user}:{password}@{host}/{db}'.format(user=MONGO_USER, password=MONGO_PASS, host=MONGO_HOST, db=MONGO_DB)

mongo = MongoClient(MONGO_ADDRESS)
db = mongo[MONGO_DB]
articles = db.articles
articles.drop()
articles.ensure_index([('sha1', 1)])


try:
    with BZ2File(args.dumpfile, 'r') as f:
        articles_count = 0
        token_articles = Counter()

        time_preproc = 0
        time_iserv = 0
        time_mongo = 0
        last_time = time()
        articles_count = 0
        this_round_count = 0

        for docgroup in grouper(args.round, parse_wiki.articles(f)):

            t1 = time()

            postings = defaultdict(lambda: [])
            bdata = index_pb.BuilderData()
            docs = []

            for doc in docgroup:
                if not doc: break

                (title, ns, sha1, text) = doc

                if ns != '0': continue
                if not text: continue # wtf
                if text.startswith('#REDIRECT'): continue


                text = unwiki(text)
                all_tokens = tokenise(text)
                tokens = normalise(all_tokens)

                if not tokens: continue

                article_tokens = Counter()

                for i, w in tokens:
                    article_tokens[w] += 1
                    postings[w] += (sha1, i)
                token_articles.update(article_tokens)

                docs.append({
                    'sha1': sha1,
                    'title': title,
                    'text': all_tokens,
                    'maxf': article_tokens.most_common(1)[0][1],
                })

            if not docs: continue

            for w, ps in postings.items():
                record = bdata.records.add()
                record.key = w
                record.value.parts.extend(map(lambda p: cPickle.dumps(p), ps))

            t2 = time()

            # Index
            iserver.feedData(bdata, deadline_ms=10)

            t3 = time()

            # MongoDB
            articles.insert(docs)

            t4 = time()

            this_round_count += len(docs)

            time_preproc += t2-t1
            time_iserv += t3-t2
            time_mongo += t4-t3


            ##
            # Reporting stats

            new_total = articles_count + this_round_count
            print('preproc: {preproc:.6f}  iserv: {iserv:.6f}  mongo: {mongo:.6f}  // +{new} articles (= {total} total)'.format(
                preproc = time_preproc / this_round_count,
                iserv = time_iserv / this_round_count,
                mongo = time_mongo / this_round_count,
                new = this_round_count,
                total = new_total,
            ))
            print('{speed:.2f} articles/s'.format(speed=this_round_count/(time()-last_time)))

            articles_count = new_total
            this_round_count = 0
            time_preproc = time_iserv = time_mongo = 0
            last_time = time()
finally:
    mongo.close()
    iserver.closeStore(index_pb.Void())
