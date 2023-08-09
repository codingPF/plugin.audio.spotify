#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
    plugin.audio.squeezebox
    spotty Player for Kodi
    utils.py
    Various helper methods
"""

import xbmc
import xbmcvfs
import xbmcgui
import os
import stat
import sys
from traceback import format_exc
import requests
import subprocess
import xbmcaddon
import struct
import time
import math
from threading import Thread, Event

PROXY_PORT = 52308
DEBUG = True

try:
    import simplejson as json
except Exception:
    import json

try:
    from cStringIO import StringIO
except ImportError:
    from io import StringIO

try:
    from cBytesIO import BytesIO
except ImportError:
    from io import BytesIO

ADDON_ID = "plugin.audio.spotify"
KODI_VERSION = int(xbmc.getInfoLabel("System.BuildVersion").split(".")[0])
KODILANGUAGE = xbmc.getLanguage(xbmc.ISO_639_1)
requests.packages.urllib3.disable_warnings()  # disable ssl warnings
SCOPE = [
        "user-read-playback-state",
        "user-read-currently-playing",
        "user-modify-playback-state",
        "playlist-read-private",
        "playlist-read-collaborative",
        "playlist-modify-public",
        "playlist-modify-private",
        "user-follow-modify",
        "user-follow-read",
        "user-library-read",
        "user-library-modify",
        "user-read-private",
        "user-read-email",
        "user-read-birthdate",
        "user-top-read"]
CLIENTID = '2eb96f9b37494be1824999d58028a305'
CLIENT_SECRET = '038ec3b4555f46eab1169134985b9013'

try:
    from multiprocessing.pool import ThreadPool

    SUPPORTS_POOL = True
except Exception:
    SUPPORTS_POOL = False


def log_msg(msg, loglevel=xbmc.LOGDEBUG):
    """log message to kodi log"""
    if isinstance(msg, str):
        msg = msg.encode('utf-8')
    if DEBUG:
        loglevel = xbmc.LOGINFO
    xbmc.log(f"{ADDON_ID} --> {msg}", level=loglevel)


def log_exception(modulename, exceptiondetails):
    """helper to properly log an exception"""
    log_msg(format_exc(sys.exc_info()), xbmc.LOGDEBUG)
    log_msg(f"Exception in {modulename} ! --> {exceptiondetails}", xbmc.LOGWARNING)


def addon_setting(settingname, set_value=None):
    """get/set addon setting"""
    addon = xbmcaddon.Addon(id=ADDON_ID)
    if set_value:
        addon.setSetting(settingname, set_value)
    else:
        return addon.getSetting(settingname)


def kill_on_timeout(done, timeout, proc):
    if not done.wait(timeout):
        proc.kill()


def get_token(spotty):
    # Get authentication token for api - prefer cached version.
    try:
        if spotty.playback_supported:
            # Try to get a token with spotty.
            token_info = request_token_spotty(spotty, use_creds=False)
            if token_info:
                # Save current username in cached spotty creds.
                spotty.get_username()
            if not token_info:
                token_info = request_token_spotty(spotty, use_creds=True)
        else:
            # Request new token with web flow.
            token_info = request_token_web()
    except Exception as exc:
        log_exception("utils.get_token", exc)
        token_info = None

    if not token_info:
        log_msg("Couldn't request authentication token. Username/password error?"
                " If you're using a facebook account with Spotify,"
                " make sure to generate a device account/password in the Spotify accountdetails.")

    return token_info


def request_token_spotty(spotty, use_creds=True):
    """request token by using the spotty binary"""
    if not spotty.playback_supported:
        return None

    token_info = None

    try:
        args = ["-t", "--client-id", CLIENTID, "--scope", ",".join(SCOPE), "-n", "temp-spotty"]
        spotty = spotty.run_spotty(arguments=args, use_creds=use_creds)

        done = Event()
        watcher = Thread(target=kill_on_timeout, args=(done, 5, spotty))
        watcher.daemon = True
        watcher.start()

        stdout, stderr = spotty.communicate()
        done.set()

        log_msg(f"request_token_spotty stdout: {stdout}")
        result = None
        for line in stdout.split():
            line = line.strip()
            if line.startswith(b"{\"accessToken\""):
                result = eval(line)

        # Transform token info to spotipy compatible format.
        if result:
            token_info = {'access_token': result['accessToken'],
                          'expires_in': result['expiresIn'],
                          'expires_at': int(time.time()) + result['expiresIn'],
                          'refresh_token': result['accessToken']
                          }
    except Exception as exc:
        log_exception(__name__, exc)

    return token_info


def request_token_web(force=False):
    """request the (initial) auth token by webbrowser"""
    import spotipy
    from spotipy import oauth2

    xbmcvfs.mkdir("special://profile/addon_data/%s/" % ADDON_ID)
    cache_path = "special://profile/addon_data/%s/spotipy.cache" % ADDON_ID
    cache_path = xbmcvfs.translatePath(cache_path)
    scope = " ".join(SCOPE)
    redirect_url = 'http://localhost:%s/callback' % PROXY_PORT
    sp_oauth = oauth2.SpotifyOAuth(CLIENTID, CLIENT_SECRET, redirect_url, scope=scope,
                                   cache_path=cache_path)
    # Get token from cache.
    token_info = sp_oauth.get_cached_token()
    if not token_info or force:
        # Request token by using the webbrowser.
        auth_url = sp_oauth.get_authorize_url()

        # Show message to user that the browser is going to be launched.
        dialog = xbmcgui.Dialog()
        header = xbmc.getInfoLabel("System.AddonTitle(%s)" % ADDON_ID)
        msg = xbmc.getInfoLabel("$ADDON[%s 11049]" % ADDON_ID)
        dialog.ok(header, msg)
        del dialog

        if xbmc.getCondVisibility("System.Platform.Android"):
            # For android we just launch the default android browser.
            xbmc.executebuiltin("StartAndroidActivity(,android.intent.action.VIEW,,%s)" % auth_url)
        else:
            # Use webbrowser module.
            import webbrowser
            log_msg("Launching system-default browser")
            webbrowser.open(auth_url, new=1)

        count = 0
        while not xbmc.getInfoLabel("Window(Home).Property(spotify-token-info)"):
            log_msg("Waiting for authentication token...")
            xbmc.sleep(2000)
            if count == 60:
                break
            count += 1

        response = xbmc.getInfoLabel("Window(Home).Property(spotify-token-info)")
        xbmc.executebuiltin("ClearProperty(spotify-token-info,Home)")
        if response:
            response = sp_oauth.parse_response_code(response)
            token_info = sp_oauth.get_access_token(response)
        xbmc.sleep(2000)  # allow enough time for the webbrowser to stop

    log_msg(f"Token from web: {token_info}", xbmc.LOGDEBUG)
    sp = spotipy.Spotify(token_info['access_token'])
    username = sp.me()["id"]
    del sp
    addon_setting("username", username)

    return token_info


def create_wave_header(duration):
    """generate a wave header for the stream"""
    file = BytesIO()
    numsamples = 44100 * duration
    channels = 2
    samplerate = 44100
    bitspersample = 16

    # Generate format chunk.
    format_chunk_spec = "<4sLHHLLHH"
    format_chunk = struct.pack(
            format_chunk_spec,
            "fmt ".encode(encoding='UTF-8'),  # Chunk id
            16,  # Size of this chunk (excluding chunk id and this field)
            1,  # Audio format, 1 for PCM
            channels,  # Number of channels
            samplerate,  # Samplerate, 44100, 48000, etc.
            samplerate * channels * (bitspersample // 8),  # Byterate
            channels * (bitspersample // 8),  # Blockalign
            bitspersample,  # 16 bits for two byte samples, etc.  => A METTRE A JOUR - POUR TEST
    )

    # Generate data chunk.
    data_chunk_spec = "<4sL"
    datasize = numsamples * channels * (bitspersample / 8)
    data_chunk = struct.pack(
            data_chunk_spec,
            "data".encode(encoding='UTF-8'),  # Chunk id
            int(datasize),  # Chunk size (excluding chunk id and this field)
    )
    sum_items = [
            # "WAVE" string following size field
            4,
            # "fmt " + chunk size field + chunk size
            struct.calcsize(format_chunk_spec),
            # Size of data chunk spec + data size
            struct.calcsize(data_chunk_spec) + datasize
    ]

    # Generate main header.
    all_chunks_size = int(sum(sum_items))
    main_header_spec = "<4sL4s"
    main_header = struct.pack(
            main_header_spec,
            "RIFF".encode(encoding='UTF-8'),
            all_chunks_size,
            "WAVE".encode(encoding='UTF-8')
    )

    # Write all the contents in.
    file.write(main_header)
    file.write(format_chunk)
    file.write(data_chunk)

    return file.getvalue(), all_chunks_size + 8


def process_method_on_list(method_to_run, items):
    """helper method that processes a method on each list item
       with pooling if the system supports it"""
    all_items = []

    if SUPPORTS_POOL:
        pool = ThreadPool()
        try:
            all_items = pool.map(method_to_run, items)
        except Exception:
            # catch exception to prevent threadpool running forever
            log_msg(format_exc(sys.exc_info()))
            log_msg("Error in %s" % method_to_run)
        pool.close()
        pool.join()
    else:
        all_items = [method_to_run(item) for item in items]

    all_items = [f for f in all_items if f]

    return all_items


def get_track_rating(popularity):
    if not popularity:
        return 0

    return int(math.ceil(popularity * 6 / 100.0)) - 1


def parse_spotify_track(track, is_album_track=True, silenced=False, is_connect=False):
    if "track" in track:
        track = track['track']
    if track.get("images"):
        thumb = track["images"][0]['url']
    elif track['album'].get("images"):
        thumb = track['album']["images"][0]['url']
    else:
        thumb = "DefaultMusicSongs"

    duration = track['duration_ms'] / 1000

    # if silenced:
    # url = "http://localhost:%s/silence/%s" % (PROXY_PORT, duration)
    # else:
    # url = "http://localhost:%s/track/%s/%s" % (PROXY_PORT, track['id'], duration)
    url = "http://localhost:%s/track/%s/%s" % (PROXY_PORT, track['id'], duration)

    if is_connect or silenced:
        url += "/?connect=true"

    if KODI_VERSION > 17:
        li = xbmcgui.ListItem(track['name'], path=url, offscreen=True)
    else:
        li = xbmcgui.ListItem(track['name'], path=url)
    infolabels = {
            "title": track['name'],
            "genre": " / ".join(track["album"].get("genres", [])),
            "year": int(track["album"].get("release_date", "0").split("-")[0]),
            "album": track['album']["name"],
            "artist": " / ".join([artist["name"] for artist in track["artists"]]),
            "rating": str(get_track_rating(track["popularity"])),
            "duration": duration
    }
    if is_album_track:
        infolabels["tracknumber"] = track["track_number"]
        infolabels["discnumber"] = track["disc_number"]
    li.setArt({"thumb": thumb})
    li.setInfo(type="Music", infoLabels=infolabels)
    li.setProperty("spotifytrackid", track['id'])
    li.setContentLookup(False)
    li.setProperty('do_not_analyze', 'true')
    li.setMimeType("audio/wave")

    return url, li


def get_chunks(data, chunksize):
    return [data[x:x + chunksize] for x in range(0, len(data), chunksize)]


def try_encode(text, encoding="utf-8"):
    try:
        return text.encode(encoding, "ignore")
    except:
        return text


def try_decode(text, encoding="utf-8"):
    try:
        return text.decode(encoding, "ignore")
    except:
        return text


def normalize_string(text):
    import unicodedata
    text = text.replace(":", "")
    text = text.replace("/", "-")
    text = text.replace("\\", "-")
    text = text.replace("<", "")
    text = text.replace(">", "")
    text = text.replace("*", "")
    text = text.replace("?", "")
    text = text.replace('|', "")
    text = text.replace('(', "")
    text = text.replace(')', "")
    text = text.replace("\"", "")
    text = text.strip()
    text = text.rstrip('.')
    text = unicodedata.normalize('NFKD', try_decode(text))
    return text


def get_playername():
    playername = xbmc.getInfoLabel("System.FriendlyName")
    if playername == "Kodi":
        import socket
        playername = "Kodi - %s" % socket.gethostname()
    return playername


class Spotty(object):
    """
        spotty is wrapped into a seperate class to store common properties
        this is done to prevent hitting a kodi issue where calling one of the infolabel methods
        at playback time causes a crash of the playback
    """

    def __init__(self):
        """initialize with default values"""
        self.__cache_path = xbmcvfs.translatePath("special://profile/addon_data/%s/" % ADDON_ID)
        self.playername = get_playername()
        self.__spotty_binary = self.get_spotty_binary()

        if self.__spotty_binary and self.test_spotty(self.__spotty_binary):
            self.playback_supported = True
            xbmc.executebuiltin("SetProperty(spotify.supportsplayback, true, Home)")
        else:
            self.playback_supported = False
            log_msg("Error while verifying spotty. Local playback is disabled.")

    @staticmethod
    def test_spotty(binary_path):
        """self-test spotty binary"""
        try:
            st = os.stat(binary_path)
            os.chmod(binary_path, st.st_mode | stat.S_IEXEC)
            args = [
                    binary_path,
                    "-n", "selftest",
                    "--disable-discovery",
                    "-x",
                    "-v"
            ]
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            spotty = subprocess.Popen(
                    args,
                    startupinfo=startupinfo,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0)

            stdout, stderr = spotty.communicate()

            log_msg(stdout)

            if "ok spotty".encode(encoding='UTF-8') in stdout:
                return True

            if xbmc.getCondVisibility("System.Platform.Windows"):
                log_msg("Unable to initialize spotty binary for playback."
                        "Make sure you have the VC++ 2015 runtime installed.", xbmc.LOGERROR)

        except Exception as exc:
            log_exception(__name__, exc)

        return False

    def run_spotty(self, arguments=None, use_creds=False, disable_discovery=False, ap_port="54443"):
        """On supported platforms we include spotty binary"""
        try:
            # os.environ["RUST_LOG"] = "debug"
            args = [
                    self.__spotty_binary,
                    "-c", self.__cache_path,
                    "-b", "320",
                    "-v",
                    "--enable-audio-cache",
                    "--ap-port", ap_port
            ]

            if disable_discovery:
                args += ["--disable-discovery"]
            if arguments:
                args += arguments
            if "-n" not in args:
                args += ["-n", self.playername]

            loggable_args = args.copy()

            if use_creds:
                # Use username/password login for spotty.
                addon = xbmcaddon.Addon(id=ADDON_ID)
                username = addon.getSetting("username")
                password = addon.getSetting("password")
                del addon
                if username and password:
                    args += ["-u", username, "-p", password]
                    loggable_args += ["-u", username, "-p", "****"]

            log_msg("run_spotty args: %s" % " ".join(loggable_args))

            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            return subprocess.Popen(args, startupinfo=startupinfo, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
        except Exception as exc:
            log_exception(__name__, exc)
        return None

    def kill_spotty(self):
        """make sure we don't have any (remaining) spotty processes running before we start one"""
        if xbmc.getCondVisibility("System.Platform.Windows"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            subprocess.Popen(["taskkill", "/IM", "spotty.exe"], startupinfo=startupinfo, shell=True)
        else:
            if self.__spotty_binary is not None:
                sp_binary_file = os.path.basename(self.__spotty_binary)
                os.system("killall " + sp_binary_file)

    def get_spotty_binary(self):
        """find the correct spotty binary belonging to the platform"""
        sp_binary = None
        if xbmc.getCondVisibility("System.Platform.Windows"):
            sp_binary = os.path.join(os.path.dirname(__file__), "spotty", "windows", "spotty.exe")
        elif xbmc.getCondVisibility("System.Platform.OSX"):
            # macos binary is x86_64 intel
            sp_binary = os.path.join(os.path.dirname(__file__), "spotty", "darwin", "spotty")
        elif xbmc.getCondVisibility("System.Platform.Linux + !System.Platform.Android"):
            # Try to find out the correct architecture by trial and error.
            import platform
            architecture = platform.machine()
            log_msg(f"reported architecture: {architecture}")
            if architecture.startswith('AMD64') or architecture.startswith('x86_64'):
                # Generic linux x86_64 binary.
                sp_binary = os.path.join(os.path.dirname(__file__), "spotty", "x86-linux",
                                         "spotty-x86_64")
            else:
                # Just try to get the correct binary path if we're unsure about the platform/cpu.
                paths = [
                        os.path.join(os.path.dirname(__file__), "spotty", "arm-linux", "spotty-hf"),
                        os.path.join(os.path.dirname(__file__), "spotty", "x86-linux", "spotty")
                ]
                for binary_path in paths:
                    if self.test_spotty(binary_path):
                        sp_binary = binary_path
                        break

        if sp_binary:
            st = os.stat(sp_binary)
            os.chmod(sp_binary, st.st_mode | stat.S_IEXEC)
            log_msg(f"Architecture detected. Using spotty binary {sp_binary}.")
        else:
            log_msg("Failed to detect architecture or platform not supported!"
                    " Local playback will not be available.")

        return sp_binary

    @staticmethod
    def get_username():
        """ obtain/check (last) username of the credentials obtained by spotify connect"""
        username = ""

        cred_file = xbmcvfs.translatePath(
                "special://profile/addon_data/%s/credentials.json" % ADDON_ID)

        if xbmcvfs.exists(cred_file):
            with open(cred_file) as cred_file:
                data = cred_file.read()
                data = eval(data)
                username = data["username"]

        addon_setting("connect_username", username)

        return username
