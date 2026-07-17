"""
Python 3 API wrapper for Garmin Connect to get your statistics.
Copy most code from https://github.com/cyberjunky/python-garminconnect
"""

import argparse
import asyncio
import datetime as dt
import logging
import os
import sys
import time
import traceback
import zipfile
from io import BytesIO
from lxml import etree

import aiofiles
import garth
import httpx
import requests
from config import FOLDER_DICT, JSON_FILE, SQL_FILE
from garmin_device_adaptor import wrap_device_info
from utils import make_activities_file

# logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

TIME_OUT = httpx.Timeout(240.0, connect=360.0)
GARMIN_COM_URL_DICT = {
    "SSO_URL_ORIGIN": "https://sso.garmin.com",
    "SSO_URL": "https://sso.garmin.com/sso",
    "MODERN_URL": "https://connectapi.garmin.com",
    "SIGNIN_URL": "https://sso.garmin.com/sso/signin",
    "UPLOAD_URL": "https://connectapi.garmin.com/upload-service/upload/",
    "ACTIVITY_URL": "https://connectapi.garmin.com/activity-service/activity/{activity_id}",
}

GARMIN_CN_URL_DICT = {
    "SSO_URL_ORIGIN": "https://sso.garmin.com",
    "SSO_URL": "https://sso.garmin.cn/sso",
    "MODERN_URL": "https://connectapi.garmin.cn",
    "SIGNIN_URL": "https://sso.garmin.cn/sso/signin",
    "UPLOAD_URL": "https://connectapi.garmin.cn/upload-service/upload/",
    "ACTIVITY_URL": "https://connectapi.garmin.cn/activity-service/activity/{activity_id}",
}


class Garmin:
    def __init__(self, secret_string, auth_domain, is_only_running=False):
        """
        Init module
        """
        self.req = httpx.AsyncClient(timeout=TIME_OUT)
        self.URL_DICT = (
            GARMIN_CN_URL_DICT
            if auth_domain and str(auth_domain).upper() == "CN"
            else GARMIN_COM_URL_DICT
        )
        if auth_domain and str(auth_domain).upper() == "CN":
            garth.configure(domain="garmin.cn")
        self.modern_url = self.URL_DICT.get("MODERN_URL")
        garth.client.loads(secret_string)

        # Refresh oauth2 token with retries and backoff to handle intermittent 429s
        if garth.client.oauth2_token.expired:
            max_retries = 5
            for attempt in range(max_retries):
                try:
                    garth.client.refresh_oauth2()
                    break
                except requests.exceptions.HTTPError as e:
                    status = None
                    try:
                        status = e.response.status_code if e.response is not None else None
                    except Exception:
                        status = None
                    # If we are rate-limited, respect Retry-After header if present; otherwise exponential backoff
                    if status == 429:
                        ra = None
                        try:
                            ra = e.response.headers.get("Retry-After") if e.response is not None else None
                        except Exception:
                            ra = None
                        wait = int(ra) if ra and str(ra).isdigit() else (2 ** attempt)
                        logger.warning(
                            "Refresh token rate limited (429). Sleeping %s seconds before retry (attempt %s/%s)",
                            wait,
                            attempt + 1,
                            max_retries,
                        )
                        time.sleep(wait)
                        continue
                    # For other errors, do a short backoff and retry a few times
                    if attempt == max_retries - 1:
                        # re-raise the last exception if we've exhausted retries
                        raise
                    time.sleep(2 ** attempt)

        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/79.0.3945.88 Safari/537.36",
            "origin": self.URL_DICT.get("SSO_URL_ORIGIN"),
            "nk": "NT",
            "Authorization": str(garth.client.oauth2_token),
        }
        self.is_only_running = is_only_running
        self.upload_url = self.URL_DICT.get("UPLOAD_URL")
        self.activity_url = self.URL_DICT.get("ACTIVITY_URL")

    async def fetch_data(self, url, retrying=False):
        """
        Fetch and return data with retries on rate limits and transient errors
        """
        retries = 5
        backoff_base = 1
        for attempt in range(retries):
            try:
                response = await self.req.get(url, headers=self.headers)
                if response.status_code == 429:
                    ra = response.headers.get("Retry-After")
                    wait = int(ra) if ra and str(ra).isdigit() else backoff_base * (2 ** attempt)
                    logger.warning(
                        "Received 429 from %s, sleeping %s seconds before retry (attempt %s/%s)",
                        url,
                        wait,
                        attempt + 1,
                        retries,
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.debug(f"fetch_data got response code {response.status_code}")
                response.raise_for_status()
                # Prefer JSON but fall back to text if the response is not JSON
                try:
                    return response.json()
                except ValueError:
                    return response.text
            except Exception as err:
                logger.debug(
                    "Exception in fetch_data (attempt %s/%s): %s", attempt + 1, retries, err
                )
                if attempt == retries - 1:
                    raise GarminConnectConnectionError("Error connecting") from err
                await asyncio.sleep(backoff_base * (2 ** attempt))

    async def get_activities(self, start, limit):
        """
        Fetch available activities
        """
        url = f"{self.modern_url}/activitylist-service/activities/search/activities?start={start}&limit={limit}"
        if self.is_only_running:
            url = url + "&activityType=running"
        return await self.fetch_data(url)

    async def get_activity_summary(self, activity_id):
        """
        Fetch activity summary
        """
        url = f"{self.modern_url}/activity-service/activity/{activity_id}"
        return await self.fetch_data(url)

    async def download_activity(self, activity_id, file_type="gpx"):
        url = f"{self.modern_url}/download-service/export/{file_type}/activity/{activity_id}"
        if file_type == "fit":
            url = f"{self.modern_url}/download-service/files/activity/{activity_id}"
        logger.info(f"Download activity from {url}")
        response = await self.req.get(url, headers=self.headers)
        response.raise_for_status()
        return response.read()

    async def upload_activities_original_from_strava(
        self, datas, use_fake_garmin_device=False
    ):
        print(
            "start upload activities to garmin!, use_fake_garmin_device:",
            use_fake_garmin_device,
        )
        for data in datas:
            print(data.filename)
            with open(data.filename, "wb") as f:
                for chunk in data.content:
                    f.write(chunk)
            f = open(data.filename, "rb")
            # wrap fake garmin device to origin fit file, current not support gpx file
            if use_fake_garmin_device:
                file_body = wrap_device_info(f)
            else:
                file_body = BytesIO(f.read())
            files = {"file": (data.filename, file_body)}

            try:
                res = await self.req.post(
                    self.upload_url, files=files, headers=self.headers
                )
                os.remove(data.filename)
                f.close()
            except Exception as e:
                print(str(e))
                # just pass for now
                continue
            try:
                resp = res.json()["detailedImportResult"]
                print("garmin upload success: ", resp)
            except Exception as e:
                print("garmin upload failed: ", e)
        await self.req.aclose()

    async def upload_activity_from_file(self, file):
        print("Uploading " + str(file))
        f = open(file, "rb")

        file_body = BytesIO(f.read())
        files = {"file": (file, file_body)}

        try:
            res = await self.req.post(
                self.upload_url, files=files, headers=self.headers
            )
            f.close()
        except Exception as e:
            print(str(e))
            # just pass for now
            return
        try:
            resp = res.json()["detailedImportResult"]
            print("garmin upload success: ", resp)
        except Exception as e:
            print("garmin upload failed: ", e)

    async def upload_activities_files(self, files):
        print("start upload activities to garmin!")

        await gather_with_concurrency(
            10,
            [self.upload_activity_from_file(file=f) for f in files],
        )

        await self.req.aclose()


class GarminConnectHttpError(Exception):
    def __init__(self, status):
        super(GarminConnectHttpError, self).__init__(status)
        self.status = status


class GarminConnectConnectionError(Exception):
    """Raised when communication ended in error."""

    def __init__(self, status):
        """Initialize."""
        super(GarminConnectConnectionError, self).__init__(status)
        self.status = status


class GarminConnectTooManyRequestsError(Exception):
    """Raised when rate limit is exceeded."""

    def __init__(self, status):
        """Initialize."""
        super(GarminConnectTooManyRequestsError, self).__init__(status)
        self.status = status


class GarminConnectAuthenticationError(Exception):
    """Raised when login returns wrong result."""

    def __init__(self, status):
        """Initialize."""
        super(GarminConnectAuthenticationError, self).__init__(status)
        self.status = status


def get_info_text_value(summary_infos, key_name):
    if summary_infos.get(key_name) is None:
        return ""
    return str(summary_infos.get(key_name))


def create_element(parent, tag, text):
    elem = etree.SubElement(parent, tag)
    elem.text = text
    elem.tail = "\n"
    return elem


def add_summary_info(file_data, summary_infos, fields=None):
    if summary_infos is None:
        return file_data
    try:
        # If file_data is not XML (e.g. an error page or plain text), skip parsing
        if isinstance(file_data, (bytes, bytearray)):
            s = file_data.lstrip()
            if not s.startswith(b"<"):
                print("File data is not XML, skipping add_summary_info")
                return file_data
        else:
            s = str(file_data).lstrip()
            if not s.startswith("<"):
                print("File data is not XML, skipping add_summary_info")
                return file_data

        root = etree.fromstring(file_data)
        extensions_node = etree.Element("extensions")
        extensions_node.text = "\n"
        extensions_node.tail = "\n"
        if fields is None:
            fields = [
                "distance",
                "average_hr",
                "average_speed",
                "start_time",
                "end_time",
                "moving_time",
                "elapsed_time",
            ]
        for field in fields:
            create_element(
                extensions_node, field, get_info_text_value(summary_infos, field)
            )
        root.insert(0, extensions_node)
        return etree.tostring(root, encoding="utf-8", pretty_print=True)
    except etree.XMLSyntaxError as e:
        print(f"Failed to parse file data: {str(e)}")
    except Exception as e:
        print(f"Failed to append summary info to file data: {str(e)}")
    return file_data


async def download_garmin_data(
    client, activity_id, file_type="gpx", summary_infos=None
):
    folder = FOLDER_DICT.get(file_type, "gpx")
    try:
        file_data = await client.download_activity(activity_id, file_type=file_type)
        if summary_infos is not None:
            file_data = add_summary_info(file_data, summary_infos.get(activity_id))
        file_path = os.path.join(folder, f"{activity_id}.{file_type}")
        need_unzip = False
        if file_type == "fit":
            file_path = os.path.join(folder, f"{activity_id}.zip")
            need_unzip = True
        async with aiofiles.open(file_path, "wb") as fb:
            await fb.write(file_data)
        if need_unzip:
            zip_file = zipfile.ZipFile(file_path, "r")
            for file_info in zip_file.infolist():
                zip_file.extract(file_info, folder)
                if file_info.filename.endswith(".fit"):
                    os.rename(
                        os.path.join(folder, f"{activity_id}_ACTIVITY.fit"),
                        os.path.join(folder, f"{activity_id}.fit"),
                    )
                elif file_info.filename.endswith(".gpx"):
                    os.rename(
                        os.path.join(folder, f"{activity_id}_ACTIVITY.gpx"),
                        os.path.join(FOLDER_DICT["gpx"], f"{activity_id}.gpx"),
                    )
                else:
                    os.remove(os.path.join(folder, file_info.filename))
            os.remove(file_path)
    except Exception as e:
        print(f"Failed to download activity {activity_id}: {str(e)}")
        traceback.print_exc()


async def get_activity_id_list(client, start=0, max_count=None):
    """Get activity ID list from Garmin, optionally limiting to max_count most recent activities."""
    activities = await client.get_activities(start, 100)
    if len(activities) > 0:
        ids = list(map(lambda a: str(a.get("activityId", "")), activities))
        print("Syncing Activity IDs")
        
        # If we've reached the desired count, stop fetching
        if max_count is not None and len(ids) >= max_count:
            return ids[:max_count]
        
        remaining = None if max_count is None else (max_count - len(ids))
        next_ids = await get_activity_id_list(client, start + 100, remaining)
        return ids + next_ids
    else:
        return []


async def gather_with_concurrency(n, tasks):
    semaphore = asyncio.Semaphore(n)

    async def sem_task(task):
        async with semaphore:
            return await task

    return await asyncio.gather(*(sem_task(task) for task in tasks))


def get_downloaded_ids(folder):
    return [i.split(".")[0] for i in os.listdir(folder) if not i.startswith(".")]


def get_garmin_summary_infos(activity_summary, activity_id):
    garmin_summary_infos = {}
    try:
        summary_dto = activity_summary.get("summaryDTO")
        garmin_summary_infos["distance"] = summary_dto.get("distance")
        garmin_summary_infos["average_hr"] = summary_dto.get("averageHR")
        garmin_summary_infos["average_speed"] = summary_dto.get("averageSpeed")
        start_time = dt.datetime.fromisoformat(
            summary_dto.get("startTimeGMT")[:-1] + "+00:00"
        )
        duration_second = summary_dto.get("duration")
        end_time = start_time + dt.timedelta(seconds=duration_second)
        garmin_summary_infos["start_time"] = start_time.isoformat()
        garmin_summary_infos["end_time"] = end_time.isoformat()
        garmin_summary_infos["moving_time"] = summary_dto.get("movingDuration")
        garmin_summary_infos["elapsed_time"] = summary_dto.get("elapsedDuration")
    except Exception as e:
        print(f"Failed to get activity summary {activity_id}: {str(e)}")
    return garmin_summary_infos


async def download_new_activities(
    secret_string, auth_domain, downloaded_ids, is_only_running, folder, file_type, max_activities=10
):
    """
    Download new activities from Garmin, limiting to max_activities most recent ones.
    """
    client = Garmin(secret_string, auth_domain, is_only_running)
    # Fetch only the most recent max_activities that haven't been downloaded yet
    activity_ids = await get_activity_id_list(client, max_count=max_activities)
    to_generate_garmin_ids = list(set(activity_ids) - set(downloaded_ids))
    print(f"Syncing Activity IDs")
    print(f"{len(to_generate_garmin_ids)} new activities to be downloaded")

    to_generate_garmin_id2title = {}
    garmin_summary_infos_dict = {}
    for id in to_generate_garmin_ids:
        try:
            activity_summary = await client.get_activity_summary(id)
            activity_title = activity_summary.get("activityName", "")
            to_generate_garmin_id2title[id] = activity_title
            garmin_summary_infos_dict[id] = get_garmin_summary_infos(
                activity_summary, id
            )
        except Exception as e:
            print(f"Failed to get activity summary {id}: {str(e)}")
            continue

    start_time = time.time()
    await gather_with_concurrency(
        10,
        [
            download_garmin_data(
                client, id, file_type=file_type, summary_infos=garmin_summary_infos_dict
            )
            for id in to_generate_garmin_ids
        ],
    )
    print(f"Download finished. Elapsed {time.time()-start_time} seconds")

    await client.req.aclose()
    return to_generate_garmin_ids, to_generate_garmin_id2title


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "secret_string", nargs="?", help="secret_string fro get_garmin_secret.py"
    )
    parser.add_argument(
        "--is-cn",
        dest="is_cn",
        action="store_true",
        help="if garmin account is cn",
    )
    parser.add_argument(
        "--only-run",
        dest="only_run",
        action="store_true",
        help="if is only for running",
    )
    parser.add_argument(
        "--tcx",
        dest="download_file_type",
        action="store_const",
        const="tcx",
        default="gpx",
        help="to download personal documents or ebook",
    )
    parser.add_argument(
        "--fit",
        dest="download_file_type",
        action="store_const",
        const="fit",
        default="gpx",
        help="to download personal documents or ebook",
    )
    options = parser.parse_args()
    secret_string = options.secret_string
    auth_domain = "CN" if options.is_cn else "COM"  # Default to COM if not specified
    file_type = options.download_file_type
    is_only_running = options.only_run
    if secret_string is None:
        print("Missing argument nor valid configuration file")
        sys.exit(1)
    folder = FOLDER_DICT.get(file_type, "gpx")
    # make gpx or tcx dir
    if not os.path.exists(folder):
        os.mkdir(folder)
    downloaded_ids = get_downloaded_ids(folder)

    if file_type == "fit":
        gpx_folder = FOLDER_DICT["gpx"]
        if not os.path.exists(gpx_folder):
            os.mkdir(gpx_folder)
        downloaded_gpx_ids = get_downloaded_ids(gpx_folder)
        # merge downloaded_ids:list
        downloaded_ids = list(set(downloaded_ids + downloaded_gpx_ids))

    loop = asyncio.get_event_loop()
    future = asyncio.ensure_future(
        download_new_activities(
            secret_string,
            auth_domain,
            downloaded_ids,
            is_only_running,
            folder,
            file_type,
            max_activities=10,
        )
    )
    loop.run_until_complete(future)
    new_ids, id2title = future.result()
    # fit may contain gpx(maybe upload by user)
    if file_type == "fit":
        make_activities_file(
            SQL_FILE,
            FOLDER_DICT["gpx"],
            JSON_FILE,
            file_suffix="gpx",
            activity_title_dict=id2title,
        )
    make_activities_file(
        SQL_FILE, folder, JSON_FILE, file_suffix=file_type, activity_title_dict=id2title
    )
