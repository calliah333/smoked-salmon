import base64
import os
import xmlrpc.client
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import asyncclick as click
import qbittorrentapi
import transmission_rpc
from deluge_client import DelugeRPCClient


@dataclass(frozen=True)
class TorrentClientTorrent:
    """Completed torrent exposed by a configured download client."""

    name: str
    hash: str
    content_path: str
    save_path: str
    category: str


def _redacted_client_url(url: str) -> str:
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    netloc = f"{hostname}:{parsed.port}" if parsed.port else hostname
    if parsed.username or parsed.password:
        netloc = f"****:****@{netloc}"

    path = parsed.path
    if "/proxy/" in path:
        prefix, _ = path.split("/proxy/", 1)
        path = f"{prefix}/proxy/****"
    return parsed._replace(netloc=netloc, path=path).geturl()


class TorrentClient:
    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        url: str | None = None,
        scheme: str | None = None,
        host: str | None = None,
        port: int | None = None,
    ):
        self.username = username
        self.password = password
        self.url = url
        self.scheme = scheme
        self.host = host
        self.port = port

        click.secho(f"Initializing {self.__class__.__name__} client...", fg="cyan")
        self.client = self.login()

    def login(self):
        raise NotImplementedError

    def add_to_downloader(self, remote_folder, torrent, is_paused, label):
        raise NotImplementedError


class QBittorrentClient(TorrentClient):
    def login(self):
        try:
            click.secho("Attempting to connect to qBittorrent...", fg="yellow")
            url = str(self.url) if self.url else ""
            qbt_client = qbittorrentapi.Client(host=url, username=self.username, password=self.password)
            qbt_client.auth_log_in()

            click.secho("Successfully connected to qBittorrent", fg="green")
            return qbt_client

        except qbittorrentapi.LoginFailed:
            click.secho("INCORRECT QBIT LOGIN CREDENTIALS", fg="red", bold=True)
            return None
        except qbittorrentapi.APIConnectionError:
            click.secho("APIConnectionError: Incorrect host or port", fg="red", bold=True)
            return None

    def search_torrents(self, query: str) -> list[TorrentClientTorrent]:
        if not self.client:
            raise click.ClickException("Could not connect to qBittorrent.")

        normalized_query = query.strip().casefold()
        torrents = []
        for torrent in self.client.torrents_info(status_filter="completed"):
            name = str(torrent.get("name") or "")
            if normalized_query not in name.casefold():
                continue
            save_path = str(torrent.get("save_path") or "")
            torrents.append(
                TorrentClientTorrent(
                    name=name,
                    hash=str(torrent.get("hash") or ""),
                    content_path=str(torrent.get("content_path") or os.path.join(save_path, name)),
                    save_path=save_path,
                    category=str(torrent.get("category") or ""),
                )
            )
        return sorted(torrents, key=lambda torrent: torrent.name.casefold())

    def add_to_downloader(self, remote_folder, torrent, is_paused, label) -> bool:
        if not self.client:
            return False

        try:
            click.secho("Adding torrent to qBittorrent...", fg="yellow")
            self.client.torrents_add(
                torrent_files=torrent, save_path=remote_folder, is_paused=is_paused, category=label
            )
            click.secho("Torrent added successfully", fg="green")
            return True
        except Exception as e:
            click.secho(f"Failed to add torrent: {e}", fg="red", bold=True)
            return False


class TransmissionClient(TorrentClient):
    def login(self) -> transmission_rpc.Client | None:
        try:
            click.secho("Attempting to connect to Transmission...", fg="yellow")
            # Cast to expected types for transmission_rpc.Client
            protocol = "https" if self.scheme == "https" else "http"
            host = str(self.host) if self.host else "localhost"
            port = int(self.port) if self.port else 9091
            trt = transmission_rpc.Client(
                protocol=protocol,
                host=host,
                port=port,
                username=self.username,
                password=self.password,
                timeout=60,
            )
            click.secho("Successfully connected to Transmission", fg="green")
            return trt
        except Exception as e:
            click.secho(f"Connect to Transmission failed: {e}", fg="red", bold=True)
            return None

    def add_to_downloader(self, remote_folder, torrent, is_paused, label):
        if not self.client:
            return None

        try:
            click.secho("Adding torrent to Transmission...", fg="yellow")
            result = self.client.add_torrent(
                torrent=torrent,
                download_dir=remote_folder,
                paused=is_paused,
                labels=([label] if label else None),
            )
            click.secho("Torrent added successfully", fg="green")
            return result
        except Exception as e:
            click.secho(f"Failed to add torrent: {e}", fg="red", bold=True)
            return None


class DelugeClient(TorrentClient):
    def login(self):
        try:
            click.secho("Attempting to connect to Deluge...", fg="yellow")
            # Cast to expected types for DelugeRPCClient
            host = str(self.host) if self.host else "localhost"
            port = int(self.port) if self.port else 58846
            de_client = DelugeRPCClient(host=host, port=port, username=self.username, password=self.password)
            de_client.connect()
            if de_client.connected is True:
                click.secho("Successfully connected to Deluge", fg="green")
                return de_client
            else:
                click.secho("Deluge connection failed: Not connected", fg="red", bold=True)
                return None
        except Exception as e:
            click.secho(f"Connect to Deluge failed: {e}", fg="red", bold=True)
            return None

    def add_to_downloader(self, remote_folder, torrent, is_paused, label):
        if not self.client:
            return None

        try:
            click.secho("Adding torrent to Deluge...", fg="yellow")
            torrent_id = os.urandom(16).hex()
            result = self.client.call(
                "core.add_torrent_file",
                f"{torrent_id}.torrent",
                base64.b64encode(torrent),
                {"download_location": remote_folder, "add_paused": is_paused},
            )

            # Set label if provided
            if label and result:
                try:
                    click.secho(f"Setting label '{label}' for torrent...", fg="yellow")
                    self.client.call("label.set_torrent", result, label)
                except Exception as label_error:
                    # If setting label failed, try to add the label first
                    if "Unknown Label" in str(label_error) or "label does not exist" in str(label_error).lower():
                        try:
                            click.secho(f"Creating label '{label}'...", fg="yellow")
                            self.client.call("label.add", label)
                            # Try setting the label again
                            self.client.call("label.set_torrent", result, label)
                            click.secho(f"Label '{label}' set successfully", fg="green")
                        except Exception as add_label_error:
                            click.secho(f"Failed to create/set label: {add_label_error}", fg="red")
                    else:
                        click.secho(f"Failed to set label: {label_error}", fg="red")
                else:
                    click.secho(f"Label '{label}' set successfully", fg="green")

            click.secho("Torrent added successfully", fg="green")
            return result
        except Exception as e:
            click.secho(f"Failed to add torrent: {e}", fg="red", bold=True)
            return None


class RuTorrentClient(TorrentClient):
    def login(self):
        try:
            url = str(self.url) if self.url else ""
            rt_client = xmlrpc.client.Server(url)
            version = rt_client.system.client_version()
            click.secho(f"Successfully connected to ruTorrent, version: {version}", fg="green")
            return rt_client
        except Exception as e:
            click.secho(f"Connect to ruTorrent failed: {e}", fg="red", bold=True)
            return None

    def add_to_downloader(self, remote_folder, torrent, is_paused, label):
        if not self.client:
            return None

        try:
            click.secho("Adding torrent to ruTorrent...", fg="yellow")
            torrent_bin = xmlrpc.client.Binary(torrent)
            commands = [
                "print=d.hash=",
                f"d.directory.set={remote_folder}",
                *([f"d.custom1.set={label}"] if label else []),
            ]

            if is_paused:
                self.client.load.raw_verbose("", torrent_bin, *commands)
            else:
                self.client.load.raw_start_verbose("", torrent_bin, *commands)

            click.secho("Torrent added successfully", fg="green")
        except Exception as e:
            click.secho(f"Failed to add torrent: {e}", fg="red", bold=True)


TORRENT_CLIENT_MAPPING = {
    "deluge": DelugeClient,
    "transmission": TransmissionClient,
    "qbittorrent": QBittorrentClient,
    "rutorrent": RuTorrentClient,
}


class TorrentClientGenerator:
    @staticmethod
    def parse_libtc_url(url: str) -> TorrentClient:
        """Parse a libtc-style URL and return the appropriate torrent client instance.

        Args:
            url: The torrent client URL in libtc format.
                Examples:
                - transmission+http://127.0.0.1:9091
                - rutorrent+http://RUTORRENT_ADDRESS:9380/plugins/rpc/rpc.php
                - deluge://username:password@127.0.0.1:58664
                - qbittorrent+http://username:password@127.0.0.1:8080

        Returns:
            An instance of the appropriate TorrentClient subclass.
        """
        parsed = urlparse(url)
        click.secho(f"\nParsing torrent client URL: {_redacted_client_url(url)}", fg="cyan")

        username: str | None = None
        password: str | None = None
        client_url: str | None = None
        scheme: str | None = None
        host: str | None = None
        port: int | None = None

        scheme_parts = parsed.scheme.split("+")
        netloc = parsed.netloc
        if "@" in netloc:
            auth, netloc = netloc.rsplit("@", 1)
            username, password = auth.split(":", 1)
            username = unquote(username)
            password = unquote(password)

        client = scheme_parts[0]
        if client in ["qbittorrent", "rutorrent"]:
            client_url = f"{scheme_parts[1]}://{netloc}{parsed.path}"
        else:
            scheme = scheme_parts[-1]  # Use last element of scheme to support deluge and transmission
            if ":" in netloc:
                host, port_str = netloc.split(":", 1)
                port = int(port_str)
            else:
                host = netloc

        return TORRENT_CLIENT_MAPPING[client](
            username=username,
            password=password,
            url=client_url,
            scheme=scheme,
            host=host,
            port=port,
        )
