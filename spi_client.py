from typing import Callable, List
import time
import threading
from bitarray import bitarray

from spi_driver_ipc import (
    b64_client_ipc as ipc,
    client_read_pipe_end,
    client_write_pipe_end,
    pack_server_command,
    unpack_server_response,
)
from spi_server import SpiServer
from obat_pc_spi_driver.spi_elements.spi_element_base import SpiElement


class SpiChannel:
    def __init__(
        self, spi_element: SpiElement, transfer_interval: float, cs: int
    ) -> None:
        self.spi_element = spi_element
        self.transfer_interval = transfer_interval
        self.cs = cs

    def __repr__(self) -> str:
        return f"SpiChannel(spi_element={self.spi_element}, transfer_interval={self.transfer_interval}, cs={self.cs}"


class SpiClient:
    """Automatic SpiClient, which instantiates its own SpiServer to send SpiCommands to."""

    def __init__(self, spi_server: SpiServer, spi_channels: List[SpiChannel]) -> None:
        self._spi_server_lock = threading.Lock()
        if len(spi_channels) < 1:
            raise ValueError("At least one SpiChannel must be specified.")
        else:
            self._spi_channels = spi_channels
            self._spi_channel_threads = [
                self._create_cyclic_locking_thread(
                    lambda: self._transfer_spi_channel(spi_channel),
                    spi_channel.transfer_interval,
                )
                for spi_channel in self._spi_channels
            ]
            self._spi_channel_threads_run_flag = False
        self._spi_server = spi_server
        self._spi_server.start_server_process()
        client_write_pipe_end.open()
        client_read_pipe_end.open()

    def __del__(self):
        self._spi_server.stop_server_process()
        client_write_pipe_end.close()
        client_read_pipe_end.close()

    def get_spi_server(self) -> SpiServer:
        return self._spi_server

    def start_cyclic_spi_channel_transfer(self) -> None:
        self._spi_channel_threads_run_flag = True
        for ch in self._spi_channel_threads:
            ch.start()

    def stop_cyclic_spi_channel_transfer(self) -> None:
        self._spi_channel_threads_run_flag = False
        for ch in self._spi_channel_threads:
            ch.join()

    def _create_cyclic_locking_thread(
        self, func: Callable[[], None], interval: float
    ) -> threading.Thread:
        def cyclic_locking_wrapper():
            last_time = time.perf_counter()
            while self._spi_channel_threads_run_flag:
                with self._spi_server_lock:
                    func()
                current_time = time.perf_counter()
                sleep_time = interval - (current_time - last_time)
                if sleep_time > 0:
                    time.sleep(sleep_time)
                last_time = current_time

        return threading.Thread(target=cyclic_locking_wrapper)

    def _write_to_spi_server(self, cs: int, buf: bytearray) -> None:
        return ipc.write(pack_server_command(cs, buf))

    def _read_from_spi_server(self) -> bytearray:
        return unpack_server_response(ipc.read())

    def _transfer_spi_channel(self, spi_channel: SpiChannel) -> None:
        op = spi_channel.spi_element.pop_unprocessed_operation()
        tx_bytearray = bytearray(op.get_command().tobytes())

        self._write_to_spi_server(spi_channel.cs, tx_bytearray)
        rx_bytearray = self._read_from_spi_server()

        if op.get_response_required():
            rsp = bitarray(len(op))
            rsp.frombytes(bytes(rx_bytearray))
            op.set_response(rsp)
        else:
            _ = rx_bytearray