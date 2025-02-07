# Copyright (c) 2021 Linux Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import os
import socket
from http import HTTPStatus
from time import sleep
from typing import List, Optional

import psycopg2
import psycopg2.extras
import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.exc import InterfaceError, OperationalError, StatementError

def isBlank (myString):
    return not (myString and myString.strip())

# Init Globals
service_name = 'ortelius-ms-dep-pkg-r'
db_conn_retry = 3

app = FastAPI(
    title=service_name,
    description=service_name
)

# Init db connection
db_host = os.getenv("DB_HOST", "localhost")
db_name = os.getenv("DB_NAME", "postgres")
db_user = os.getenv("DB_USER", "postgres")
db_pass = os.getenv("DB_PASS", "postgres")
db_port = os.getenv("DB_PORT", "5432")
validateuser_url = os.getenv('VALIDATEUSER_URL', None )

if (validateuser_url is None):
    validateuser_host = os.getenv('MS_VALIDATE_USER_SERVICE_HOST', '127.0.0.1')
    host = socket.gethostbyaddr(validateuser_host)[0]
    validateuser_url = 'http://' + host + ':' + str(os.getenv('MS_VALIDATE_USER_SERVICE_PORT', 80))

engine = create_engine("postgresql+psycopg2://" + db_user + ":" + db_pass + "@" + db_host + ":" + db_port + "/" + db_name, pool_pre_ping=True)

# health check endpoint


class StatusMsg(BaseModel):
    status: str
    service_name: Optional[str] = None


@app.get("/health",
         responses={
             503: {"model": StatusMsg,
                   "description": "DOWN Status for the Service",
                   "content": {
                       "application/json": {
                           "example": {"status": 'DOWN'}
                       },
                   },
                   },
             200: {"model": StatusMsg,
                   "description": "UP Status for the Service",
                   "content": {
                       "application/json": {
                           "example": {"status": 'UP', "service_name": service_name}
                       }
                   },
                   },
         }
         )
async def health(response: Response):
    try:
        with engine.connect() as connection:
            conn = connection.connection
            cursor = conn.cursor()
            cursor.execute('SELECT 1')
            if cursor.rowcount > 0:
                return {"status": 'UP', "service_name": service_name}
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": 'DOWN'}

    except Exception as err:
        print(str(err))
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": 'DOWN'}
# end health check


class DepPkg(BaseModel):
    packagename: str
    packageversion: str
    name: str
    url: str
    summary: str
    fullcompname: str
    risklevel: str


class DepPkgs(BaseModel):
    data: List[DepPkg]


class Message(BaseModel):
    detail: str


@app.get('/msapi/deppkg',
         responses={
             401: {"model": Message,
                   "description": "Authorization Status",
                   "content": {
                       "application/json": {
                           "example": {"detail": "Authorization failed"}
                       },
                   },
                   },
             500: {"model": Message,
                   "description": "SQL Error",
                   "content": {
                       "application/json": {
                           "example": {"detail": "SQL Error: 30x"}
                       },
                   },
                   },
             200: {
                 "model": DepPkgs,
                 "description": "Component Paackage Dependencies"},
             "content": {
                 "application/json": {
                     "example": {"data": [{"packagename": "Flask", "packageversion": "1.2.2", "name": "BSD-3-Clause", "url": "https://spdx.org/licenses/BSD-3-Clause.html", "summary": ""}]}
                 }
             }
         }
         )
async def getCompPkgDeps(request: Request, compid: Optional[int] = None, appid: Optional[int] = None, deptype: str = Query(..., regex="(?:license|cve)")):
    try:
        result = requests.get(validateuser_url + "/msapi/validateuser", cookies=request.cookies)
        if (result is None):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization Failed")

        if (result.status_code != status.HTTP_200_OK):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization Failed status_code=" + str(result.status_code))
    except Exception as err:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization Failed:" + str(err)) from None

    response_data = []

    try:
        #Retry logic for failed query
        no_of_retry = db_conn_retry
        attempt = 1;
        while True:
            try:
                with engine.connect() as connection:
                    conn = connection.connection
                    cursor = conn.cursor()
        
                    sql = ""
                    id = compid
                    if (compid is not None):
                        sql = "SELECT packagename, packageversion, name, url, summary, '', purl, pkgtype FROM dm_componentdeps where compid = %s and deptype = %s"
                    elif (appid is not None):
                        sql = "select distinct b.packagename, b.packageversion, b.name, b.url, b.summary, fulldomain(c.domainid, c.name), b.purl, b.pkgtype from dm.dm_applicationcomponent a, dm.dm_componentdeps b, dm.dm_component c where appid = %s and a.compid = b.compid and c.id = b.compid and b.deptype = %s"
                        id = appid
        
                    params = tuple([id, 'license'])
                    cursor.execute(sql, params)
                    rows = cursor.fetchall()
                    valid_url = {}
        
                    for row in rows:
                        packagename = row[0]
                        packageversion = row[1]
                        name = row[2]
                        url = row[3]
                        summary = row[4]
                        fullcompname = row[5]
                        purl = row[6]
                        pkgtype = row[7]
        
                        if (deptype == "license"):
                            if (not url):
                                url = 'https://spdx.org/licenses/'
            
                            # check for license on SPDX site if not found just return the license landing page
                            if (name not in valid_url):
                                r = requests.head(url)
                                if (r.status_code == 200):
                                    valid_url[name] = url
                                else:
                                    valid_url[name] = 'https://spdx.org/licenses/'
            
                            url = valid_url[name]
            
                            response_data.append(
                                {
                                    'packagename': packagename,
                                    'packageversion': packageversion,
                                    'name': name,
                                    'url': url,
                                    'summary': summary,
                                    'fullcompname': fullcompname
                                }
                            )
                        else:
                            v_sql = ""
                            v_params = tuple([])
                            if (isBlank(purl)):
                                v_sql = "select id, summary, risklevel from dm.dm_vulns where packagename = %s and packageversion = %s"
                                v_params = tuple([packagename, packageversion])
                            else:
                                if ('?' in purl):
                                    purl = purl.split('?')[0]
                                v_sql = "select id, summary,risklevel from dm.dm_vulns where purl = %s"
                                v_params = tuple([purl])

                            v_cursor = conn.cursor()
                            v_cursor.execute(v_sql, v_params)
                            v_rows = v_cursor.fetchall()

                            for v_row in v_rows:
                                id = v_row[0]
                                summary = v_row[1]
                                risklevel = v_row[2]

                                url = "https://osv.dev/vulnerability/" + id
                                response_data.append(
                                    {
                                        'packagename': packagename,
                                        'packageversion': packageversion,
                                        'name': id,
                                        'url': url,
                                        'summary': summary,
                                        'fullcompname': fullcompname,
                                        'risklevel': risklevel
                                    }
                                )
                            v_cursor.close()

                    cursor.close()
                    return {'data': response_data}
            
            except (InterfaceError, OperationalError) as ex:
                if attempt < no_of_retry:
                    sleep_for = 0.2
                    logging.error(
                        "Database connection error: {} - sleeping for {}s"
                        " and will retry (attempt #{} of {})".format(
                            ex, sleep_for, attempt, no_of_retry
                        )
                    )
                    #200ms of sleep time in cons. retry calls 
                    sleep(sleep_for) 
                    attempt += 1
                    continue
                else:
                    raise
        
    except HTTPException:
        raise
    except Exception as err:
        print(str(err))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(err)) from None

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5004)
