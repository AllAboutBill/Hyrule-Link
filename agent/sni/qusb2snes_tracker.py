"""QUsb2Snes Tracker for ALttPR
Direct connection to Qusb2snes without SNI.
Compatible interface with SNITracker.
"""
import json
import time
import websocket
import threading
from typing import Optional, Dict, List, Tuple
from queue import Queue
import sys
import os
import logging

# Set up logger
logger = logging.getLogger("QUsb2Snes")

# Import memory addresses from local constants (standalone app)
try:
    from .memory_constants import MEMORY_ADDRESSES
except ImportError:
    from memory_constants import MEMORY_ADDRESSES

class QUsb2SnesTracker:
    def __init__(self, host='localhost', port=23074, debug=False):
        """
        Initialize QUsb2Snes tracker.
        
        Args:
            host: QUsb2Snes host (default: localhost)
            port: QUsb2Snes port (default: 23074 - this is Qusb2snes's WebSocket port)
                  Note: Port 8080 is used internally by Qusb2snes to connect to emulators via EmuNWA
            debug: Enable debug logging
        """
        self.host = host
        self.port = port
        self.debug = debug
        self.ws = None
        self.ws_app = None
        self.connected = False
        self.device_uri = None
        self.response_queue = Queue()
        self.command_lock = threading.Lock()
        self.ws_thread = None
        
        # Track previous values (same as SNI version)
        self.previous_values = {
            'game_mode': None,
            'death_count': None,
            'half_magic': None,
            'items': {}
        }
        
        # Death event tracking
        self.death_events = {
            'death_start_emitted': False,
            'last_death_count': None
        }
        
        # Track all obtained items
        self.obtained_items = set()
        
        # Reconnection handling
        self._consecutive_failures = 0
        self._max_consecutive_failures = 5
        self._last_successful_read = None
    
    def connect(self) -> bool:
        """Connect to QUsb2Snes WebSocket server."""
        try:
            url = f"ws://{self.host}:{self.port}"
            logger.info(f"Connecting to {url}...")
            
            # Test if port is accessible first
            import socket
            try:
                test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                test_socket.settimeout(2)
                result = test_socket.connect_ex((self.host, self.port))
                test_socket.close()
                if result != 0:
                    logger.debug(f"Port {self.port} is not accessible. Is Qusb2snes running?")
                    return False
            except Exception as e:
                logger.debug(f"Could not test port accessibility: {e}")
                # Continue anyway
            
            # Create WebSocket app
            self.ws_app = websocket.WebSocketApp(
                url,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
                on_open=self._on_open
            )
            
            # Start WebSocket in a separate thread
            self.ws_thread = threading.Thread(target=self._run_websocket, daemon=True)
            self.ws_thread.start()
            
            # Wait for connection (with timeout)
            timeout = 5
            start = time.time()
            while not self.connected and (time.time() - start) < timeout:
                time.sleep(0.1)
            
            if not self.connected:
                logger.debug(f"Connection timeout after {timeout}s - WebSocket did not open")
                return False
            
            logger.debug("WebSocket opened, listing devices...")
            
            # List devices and attach to first one
            devices = self.list_devices()
            if not devices:
                logger.debug("No devices found. Make sure your emulator is connected to Qusb2snes.")
                return False
            
            logger.info(f"Found {len(devices)} device(s): {devices}")
            self.device_uri = devices[0]
            
            # Send Name command to identify ourselves (required by usb2snes protocol)
            if self.debug:
                logger.debug("Identifying as TwitchBot...")
            self._send_command("Name", operands=["TwitchBot"], wait_for_response=False)
            time.sleep(0.1)
            
            if self.debug:
                logger.debug(f"Attaching to device: {self.device_uri}")
            if self.attach_device(self.device_uri):
                logger.info(f"Connected and attached to {self.device_uri}")
                return True
            else:
                logger.warning(f"Failed to attach to device {self.device_uri}")
                return False
            
        except Exception as e:
            logger.error(f"Connection error: {e}")
            if self.debug:
                import traceback
                traceback.print_exc()
            return False
    
    def _run_websocket(self):
        """Run WebSocket in thread."""
        try:
            self.ws_app.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            logger.debug(f"WebSocket thread error: {e}")
            if self.debug:
                import traceback
                traceback.print_exc()
    
    def _on_open(self, ws):
        """WebSocket connection opened."""
        self.ws = ws
        self.connected = True
        if self.debug:
            logger.debug("WebSocket connected successfully")
    
    def _on_message(self, ws, message):
        """Handle incoming WebSocket messages."""
        try:
            # usb2snes uses binary data for GetAddress responses
            if isinstance(message, bytes):
                # Binary response - put in queue
                self.response_queue.put(('binary', message))
            else:
                # JSON response
                try:
                    data = json.loads(message)
                    self.response_queue.put(('json', data))
                except:
                    # Sometimes it's a string response
                    self.response_queue.put(('json', {'Results': [message]}))
        except Exception as e:
            if self.debug:
                print(f"[QUsb2Snes] Error handling message: {e}")
    
    def _on_error(self, ws, error):
        """Handle WebSocket errors."""
        logger.debug(f"WebSocket error: {error}")
        if self.debug:
            import traceback
            if hasattr(error, '__traceback__'):
                traceback.print_exception(type(error), error, error.__traceback__)
        self._consecutive_failures += 1
        self.connected = False
    
    def _on_close(self, ws, close_status_code, close_msg):
        """WebSocket connection closed."""
        self.connected = False
        if self.debug:
            logger.debug(f"WebSocket closed: {close_status_code}")
    
    def _send_command(self, opcode: str, space: str = "SNES", operands: List[str] = None, wait_for_response=True) -> Optional[dict]:
        """Send a command to QUsb2Snes and wait for response."""
        if not self.connected or not self.ws:
            if self.debug:
                logger.debug(f"Cannot send command {opcode} - not connected")
            return None
        
        with self.command_lock:
            command = {
                "Opcode": opcode,
                "Space": space
            }
            if operands:
                command["Operands"] = operands
            
            try:
                # Send JSON command
                cmd_json = json.dumps(command)
                if self.debug:
                    logger.debug(f"Sending command: {cmd_json}")
                self.ws.send(cmd_json)
                
                if not wait_for_response:
                    return {'Results': ['OK']}
                
                # Wait for response (with timeout)
                timeout = 3.0  # Increased timeout
                start = time.time()
                while (time.time() - start) < timeout:
                    try:
                        msg_type, data = self.response_queue.get(timeout=0.2)
                        if self.debug:
                            logger.debug(f"Received {msg_type} response for {opcode}")
                        if msg_type == 'json':
                            self._consecutive_failures = 0
                            self._last_successful_read = time.time()
                            return data
                        elif msg_type == 'binary':
                            self._consecutive_failures = 0
                            self._last_successful_read = time.time()
                            return {'data': data}
                    except:
                        continue
                
                if self.debug:
                    logger.debug(f"Command {opcode} timed out after {timeout}s")
                return None
                
            except Exception as e:
                logger.debug(f"Error sending command {opcode}: {e}")
                if self.debug:
                    import traceback
                    traceback.print_exc()
                self._consecutive_failures += 1
                return None
    
    def list_devices(self) -> List[str]:
        """List available devices."""
        if self.debug:
            logger.debug("Requesting device list...")
        response = self._send_command("DeviceList")
        if response and 'Results' in response:
            devices = response['Results']
            if self.debug:
                logger.debug(f"DeviceList response: {devices}")
            return devices
        if self.debug:
            logger.debug(f"DeviceList failed or empty response: {response}")
        return []
    
    def attach_device(self, uri: str) -> bool:
        """Attach to a device."""
        if self.debug:
            logger.debug(f"Attempting to attach to device: {uri}")
        # Attach command in usb2snes protocol doesn't return a response
        # It just attaches and continues - no JSON response is sent
        # Some implementations may close the WebSocket after Attach, requiring reconnection
        try:
            # Check if WebSocket is still connected before sending Attach
            if not self.connected or not self.ws:
                logger.warning("[QUsb2Snes] Cannot attach: WebSocket not connected")
                return False
            
            # Clear any stale messages (e.g. Name response) so verification doesn't eat wrong reply
            while not self.response_queue.empty():
                try:
                    self.response_queue.get_nowait()
                except Exception:
                    break
            
            with self.command_lock:
                command = {
                    "Opcode": "Attach",
                    "Space": "SNES",
                    "Operands": [uri]
                }
                cmd_json = json.dumps(command)
                if self.debug:
                    logger.debug(f"Sending Attach command: {cmd_json}")
                try:
                    self.ws.send(cmd_json)
                    if self.debug:
                        logger.debug("Attach command sent")
                except Exception as send_error:
                    logger.warning(f"[QUsb2Snes] Error sending Attach: {send_error}")
                    return False
            
            # Give the server/emulator time to process (EmuNWA/Snes9x can be slow)
            time.sleep(1.0)
            
            # Check if WebSocket closed after Attach (some implementations do this)
            if not self.connected or not self.ws:
                if self.debug:
                    logger.debug("WebSocket closed after Attach - reconnecting and re-attaching...")
                
                if not self._reconnect_websocket():
                    logger.warning("[QUsb2Snes] Reconnect after Attach failed - is another app using the device?")
                    return False
                
                # Re-send Attach on the new connection (device is not attached on this socket yet)
                time.sleep(0.3)
                with self.command_lock:
                    cmd = {"Opcode": "Attach", "Space": "SNES", "Operands": [uri]}
                    try:
                        self.ws.send(json.dumps(cmd))
                    except Exception as send_err:
                        logger.warning(f"[QUsb2Snes] Re-attach send failed: {send_err}")
                        return False
                time.sleep(0.5)
            
            # Verify: try Info first (works even when no game is loaded), then memory read
            while not self.response_queue.empty():
                try:
                    self.response_queue.get_nowait()
                except Exception:
                    break
            
            info_response = self._send_command("Info", wait_for_response=True)
            if info_response is not None and info_response.get("Results"):
                if self.debug:
                    logger.debug(f"Attach verified via Info: {info_response['Results']}")
                return True
            
            test_read = self.read_memory(0x0010, size=1)  # Read game mode
            if test_read is not None and len(test_read) >= 1:
                if self.debug:
                    logger.debug(f"Attach verified via memory read: 0x{test_read[0]:02X}")
                return True
            
            # Both failed - still accept so subsequent ops can retry (e.g. game not loaded yet)
            if self.debug:
                logger.debug("Attach verification failed (Info + read); continuing anyway.")
            return True
            
        except Exception as e:
            logger.warning(f"[QUsb2Snes] Attach failed: {e}")
            if self.debug:
                import traceback
                traceback.print_exc()
            return False
    
    def _reconnect_websocket(self) -> bool:
        """Reconnect WebSocket if it closed."""
        try:
            # Stop existing WebSocket if running
            if self.ws_app:
                try:
                    if self.ws:
                        self.ws.close()
                except:
                    pass
            
            # Create new WebSocket connection
            url = f"ws://{self.host}:{self.port}"
            if self.debug:
                logger.debug(f"Reconnecting to {url}...")
            
            self.ws_app = websocket.WebSocketApp(
                url,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
                on_open=self._on_open
            )
            
            # Start WebSocket in a separate thread
            if self.ws_thread and self.ws_thread.is_alive():
                # Wait for old thread to finish
                self.ws_thread.join(timeout=2)
            
            self.ws_thread = threading.Thread(target=self._run_websocket, daemon=True)
            self.ws_thread.start()
            
            # Wait for connection (with timeout)
            timeout = 5
            start = time.time()
            while not self.connected and (time.time() - start) < timeout:
                time.sleep(0.1)
            
            if not self.connected:
                if self.debug:
                    logger.debug(f"Reconnection timeout after {timeout}s")
                return False
            
            if self.debug:
                logger.debug("WebSocket reconnected successfully")
            
            # Re-send Name command to identify ourselves
            if self.debug:
                logger.debug("Re-identifying as TwitchBot...")
            self._send_command("Name", operands=["TwitchBot"], wait_for_response=False)
            time.sleep(0.1)
            
            return True
            
        except Exception as e:
            logger.debug(f"Error reconnecting WebSocket: {e}")
            if self.debug:
                import traceback
                traceback.print_exc()
            return False
    
    def read_memory(self, address: int, size: int = 1, domain: str = 'WRAM') -> Optional[bytes]:
        """
        Read memory from SNES WRAM.
        
        Args:
            address: WRAM offset address (e.g., 0xF416)
            size: Number of bytes to read
            domain: Ignored (for compatibility with SNI interface)
        
        Returns:
            Bytes read, or None if failed
        """
        if not self.connected or not self.ws:
            # Silent return - let caller handle the None
            return None
        
        # Convert WRAM offset to SD2SNES/FXPak address space (0xF50000 + offset)
        # Note: EmuNWA/Qusb2snes uses SD2SNES convention, not direct SNES addresses
        # 0xF50000-0xF5FFFF maps to WRAM (equivalent to 0x7E0000-0x7EFFFF)
        snes_address = 0xF50000 + address
        
        # Format as hex string (no 0x prefix, uppercase for usb2snes)
        addr_hex = f"{snes_address:06X}"
        size_hex = f"{size:X}"
        
        if self.debug:
            print(f"[QUsb2Snes] Reading {size} byte(s) from 0x{addr_hex} (WRAM offset 0x{address:04X})")
        
        try:
            with self.command_lock:
                # Send GetAddress command
                cmd = {
                    "Opcode": "GetAddress",
                    "Space": "SNES",
                    "Operands": [addr_hex, size_hex]
                }
                if self.debug:
                    print(f"[QUsb2Snes] Sending: {cmd}")
                self.ws.send(json.dumps(cmd))
                
                # Wait for binary response
                timeout = 2.0
                start = time.time()
                while (time.time() - start) < timeout:
                    try:
                        msg_type, data = self.response_queue.get(timeout=0.1)
                        if self.debug:
                            print(f"[QUsb2Snes] Got response type: {msg_type}, len: {len(data) if data else 0}")
                        if msg_type == 'binary':
                            # Return only the requested size
                            result = bytes(data[:size])
                            self._consecutive_failures = 0
                            self._last_successful_read = time.time()
                            if self.debug:
                                print(f"[QUsb2Snes] Read success: {result.hex()}")
                            return result
                        elif msg_type == 'json':
                            # Might be an error response
                            print(f"[QUsb2Snes] Got JSON instead of binary: {data}")
                    except:
                        continue
                
                if self.debug:
                    logger.debug(f"Read timeout for address 0x{address:04X} (SNES 0x{addr_hex})")
                self._consecutive_failures += 1
                return None
        except Exception as e:
            if self.debug:
                logger.debug(f"Error reading memory 0x{address:04X}: {e}")
                import traceback
                traceback.print_exc()
            self._consecutive_failures += 1
            return None
    
    def write_memory(self, address: int, data: bytes, domain: str = 'WRAM') -> bool:
        """
        Write memory to SNES WRAM.
        
        Args:
            address: WRAM offset address
            data: Bytes to write
            domain: Ignored (for compatibility with SNI interface)
        
        Returns:
            True if successful
        """
        if not self.connected or not self.ws:
            # Silent return - let caller handle the False
            return False
        
        # Convert WRAM offset to SD2SNES/FXPak address space (0xF50000 + offset)
        # Note: EmuNWA/Qusb2snes uses SD2SNES convention, not direct SNES addresses
        snes_address = 0xF50000 + address
        
        # Format as hex string
        addr_hex = f"{snes_address:06X}"
        size_hex = f"{len(data):X}"
        
        if self.debug:
            print(f"[QUsb2Snes] Writing {len(data)} byte(s) to 0x{addr_hex} (WRAM offset 0x{address:04X}): {data.hex()}")
        
        try:
            with self.command_lock:
                # Send PutAddress command with address and size
                cmd = {
                    "Opcode": "PutAddress",
                    "Space": "SNES",
                    "Operands": [addr_hex, size_hex]
                }
                if self.debug:
                    print(f"[QUsb2Snes] Sending: {cmd}")
                self.ws.send(json.dumps(cmd))
                
                # Send binary data
                if self.debug:
                    print(f"[QUsb2Snes] Sending binary data: {data.hex()}")
                self.ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
                
                # Wait a bit for processing
                time.sleep(0.02)
                
                self._consecutive_failures = 0
                self._last_successful_read = time.time()
                
                if self.debug:
                    print(f"[QUsb2Snes] Write completed to 0x{addr_hex}")
                return True
                
        except Exception as e:
            if self.debug:
                logger.debug(f"Error writing memory 0x{address:04X}: {e}")
                import traceback
                traceback.print_exc()
            self._consecutive_failures += 1
            return False
    
    def reset_console(self) -> bool:
        """
        Reset the console/emulator.
        
        Returns:
            True if successful
        """
        if not self.connected or not self.ws:
            if self.debug:
                logger.debug("Cannot reset console - not connected")
            return False
        
        if self.debug:
            logger.debug("Resetting console...")
        response = self._send_command("Reset", wait_for_response=False)
        
        if response is not None:
            if self.debug:
                logger.debug("Reset command sent")
            time.sleep(0.5)  # Give it a moment to process
            return True
        else:
            if self.debug:
                logger.debug("Failed to send reset command")
            return False
    
    def disconnect(self):
        """Disconnect from QUsb2Snes."""
        logger.info("Disconnecting from QUsb2Snes...")
        if self.ws_app:
            try:
                self._send_command("Close", wait_for_response=False)
            except:
                pass
            try:
                if self.ws:
                    self.ws.close()
            except:
                pass
            try:
                if self.ws_app:
                    self.ws_app.close()
            except:
                pass
        self.connected = False
        logger.info("Disconnected from QUsb2Snes")
    
    # Compatibility methods - same interface as SNITracker
    def _set_slot_with_selection(self, inventory_address: int, inventory_value: int, selected_index: int) -> bool:
        """Helper method to update both inventory slot and selected item index."""
        selected_addr = MEMORY_ADDRESSES.get('selected_item_index', 0xF33F)
        
        # Write inventory slot
        inv_success = self.write_memory(inventory_address, bytes([inventory_value]))
        
        # Write selected index
        sel_success = self.write_memory(selected_addr, bytes([selected_index]))
        
        if inv_success and sel_success:
            self.previous_values['items'][inventory_address] = inventory_value
            if self.debug:
                print(f"[QUsb2Snes] Updated slot 0x{inventory_address:04X}=0x{inventory_value:02X}, selected_index=0x{selected_index:02X}")
            return True
        else:
            if self.debug:
                print(f"[QUsb2Snes] Failed to update slot/selection: inv={inv_success}, sel={sel_success}")
            return False
    
    def _validate_selected_index(self, inventory_address: int, inventory_value: int) -> int:
        """Validate and return a valid selected index for the given inventory slot."""
        selected_addr = MEMORY_ADDRESSES.get('selected_item_index', 0xF33F)
        current_sel_data = self.read_memory(selected_addr, size=1)
        current_sel = int(current_sel_data[0]) if current_sel_data and len(current_sel_data) >= 1 else 0
        
        # Check if current selection is valid for this slot
        if inventory_address == 0xF341:  # Boomerangs
            if inventory_value & 0x01 and current_sel == 0x08:
                return 0x08
            if inventory_value & 0x02 and current_sel == 0x09:
                return 0x09
            if inventory_value & 0x01:
                return 0x08
            if inventory_value & 0x02:
                return 0x09
        elif inventory_address == 0xF344:  # Mushroom/Powder
            if inventory_value == 0x01 and current_sel == 0x0A:
                return 0x0A
            if inventory_value == 0x02 and current_sel == 0x0B:
                return 0x0B
            if inventory_value == 0x01:
                return 0x0A
            if inventory_value == 0x02:
                return 0x0B
        elif inventory_address == 0xF34C:  # Shovel/Flute
            if inventory_value == 0x01 and current_sel == 0x0D:
                return 0x0D
            if (inventory_value == 0x02 or inventory_value == 0x03) and current_sel == 0x0E:
                return 0x0E
            if inventory_value == 0x01:
                return 0x0D
            if inventory_value == 0x02 or inventory_value == 0x03:
                return 0x0E
        
        return current_sel
    
    def _remove_boomerang(self, blue: bool = False, red: bool = False) -> bool:
        """Remove boomerang(s) using bitwise AND to preserve other boomerangs."""
        address = 0xF341
        current_data = self.read_memory(address, size=1)
        if not current_data or len(current_data) < 1:
            current_value = 0
        else:
            current_value = int(current_data[0])
        
        new_value = current_value
        if blue:
            new_value &= ~0x01
        if red:
            new_value &= ~0x02
        
        selected_index = 0x08
        if new_value & 0x01:
            selected_index = 0x08
        elif new_value & 0x02:
            selected_index = 0x09
        else:
            selected_index = self._validate_selected_index(address, new_value)
        
        success = self._set_slot_with_selection(address, new_value, selected_index)
        if success:
            print(f"[QUsb2Snes] Removed boomerang(s): blue={blue}, red={red}, value=0x{new_value:02X}")
        return success
    
    def _remove_mushroom_powder(self, mushroom: bool = False, powder: bool = False) -> bool:
        """Remove mushroom/powder using bitwise AND to preserve the other."""
        address = 0xF344
        current_data = self.read_memory(address, size=1)
        if not current_data or len(current_data) < 1:
            current_value = 0
        else:
            current_value = int(current_data[0])
        
        new_value = current_value
        if mushroom:
            new_value &= ~0x01
        if powder:
            new_value &= ~0x02
        
        selected_index = 0x0A
        if new_value == 0x01:
            selected_index = 0x0A
        elif new_value == 0x02:
            selected_index = 0x0B
        else:
            selected_index = self._validate_selected_index(address, new_value)
        
        success = self._set_slot_with_selection(address, new_value, selected_index)
        if success:
            print(f"[QUsb2Snes] Removed mushroom/powder: mushroom={mushroom}, powder={powder}, value=0x{new_value:02X}")
        return success
    
    def _remove_shovel_flute(self, shovel: bool = False, flute: bool = False) -> bool:
        """Remove shovel/flute using bitwise AND to preserve the other."""
        address = 0xF34C
        current_data = self.read_memory(address, size=1)
        if not current_data or len(current_data) < 1:
            current_value = 0
        else:
            current_value = int(current_data[0])
        
        new_value = current_value
        if shovel:
            new_value &= ~0x01
        if flute:
            if current_value == 0x03:
                new_value = 0x02
                selected_index = 0x0D
                success = self._set_slot_with_selection(address, new_value, selected_index)
                if success:
                    print(f"[QUsb2Snes] Deactivated flute: value=0x{new_value:02X}")
                return success
            else:
                new_value &= ~0x03
        
        selected_index = 0x0D
        if new_value == 0x01:
            selected_index = 0x0D
        elif new_value == 0x02 or new_value == 0x03:
            selected_index = 0x0E
        else:
            selected_index = self._validate_selected_index(address, new_value)
        
        success = self._set_slot_with_selection(address, new_value, selected_index)
        if success:
            print(f"[QUsb2Snes] Removed shovel/flute: shovel={shovel}, flute={flute}, value=0x{new_value:02X}")
        return success
    
    def _add_boomerang(self, blue: bool = False, red: bool = False) -> bool:
        """Add boomerang(s) using bitwise OR to preserve existing boomerangs."""
        address = 0xF341
        current_data = self.read_memory(address, size=1)
        if not current_data or len(current_data) < 1:
            current_value = 0
        else:
            current_value = int(current_data[0])
        
        new_value = current_value
        if blue:
            new_value |= 0x01
        if red:
            new_value |= 0x02
        
        selected_index = 0x08
        if blue:
            selected_index = 0x08
        elif red:
            selected_index = 0x09
        elif new_value & 0x01:
            selected_index = 0x08
        elif new_value & 0x02:
            selected_index = 0x09
        
        success = self._set_slot_with_selection(address, new_value, selected_index)
        if success:
            print(f"[QUsb2Snes] Added boomerang(s): blue={blue}, red={red}, value=0x{new_value:02X}")
        return success
    
    def _add_mushroom_powder(self, mushroom: bool = False, powder: bool = False) -> bool:
        """Add mushroom/powder using bitwise OR to allow both."""
        address = 0xF344
        current_data = self.read_memory(address, size=1)
        if not current_data or len(current_data) < 1:
            current_value = 0
        else:
            current_value = int(current_data[0])
        
        new_value = current_value
        if mushroom:
            new_value |= 0x01
        if powder:
            new_value |= 0x02
        
        selected_index = 0x0A
        if mushroom:
            selected_index = 0x0A
        elif powder:
            selected_index = 0x0B
        elif new_value == 0x01:
            selected_index = 0x0A
        elif new_value == 0x02:
            selected_index = 0x0B
        
        success = self._set_slot_with_selection(address, new_value, selected_index)
        if success:
            print(f"[QUsb2Snes] Added mushroom/powder: mushroom={mushroom}, powder={powder}, value=0x{new_value:02X}")
        return success
    
    def _add_shovel_flute(self, shovel: bool = False, flute: bool = False) -> bool:
        """Add shovel/flute using bitwise OR to allow both."""
        address = 0xF34C
        current_data = self.read_memory(address, size=1)
        if not current_data or len(current_data) < 1:
            current_value = 0
        else:
            current_value = int(current_data[0])
        
        new_value = current_value
        if shovel:
            new_value |= 0x01
        if flute:
            new_value |= 0x03
        
        selected_index = 0x0D
        if flute:
            selected_index = 0x0E
        elif shovel:
            selected_index = 0x0D
        elif new_value == 0x01:
            selected_index = 0x0D
        elif new_value == 0x02 or new_value == 0x03:
            selected_index = 0x0E
        
        success = self._set_slot_with_selection(address, new_value, selected_index)
        if success:
            print(f"[QUsb2Snes] Added shovel/flute: shovel={shovel}, flute={flute}, value=0x{new_value:02X}")
        return success
    
    def remove_item(self, item_name: str) -> bool:
        """Remove an item from inventory."""
        if item_name == 'blue_boomerang':
            return self._remove_boomerang(blue=True)
        elif item_name == 'red_boomerang':
            return self._remove_boomerang(red=True)
        elif item_name == 'mushroom':
            return self._remove_mushroom_powder(mushroom=True)
        elif item_name == 'powder':
            return self._remove_mushroom_powder(powder=True)
        elif item_name == 'shovel':
            return self._remove_shovel_flute(shovel=True)
        elif item_name == 'flute':
            return self._remove_shovel_flute(flute=True)
        
        item_address = None
        for addr, name in MEMORY_ADDRESSES.get('items_inventory', {}).items():
            if name == item_name:
                item_address = addr
                break
        
        if item_address is None:
            print(f"[QUsb2Snes] Item '{item_name}' not found in inventory addresses")
            return False
        
        success = self.write_memory(item_address, bytes([0]))
        if success:
            self.previous_values['items'][item_address] = 0
            if item_name in self.obtained_items:
                self.obtained_items.remove(item_name)
            print(f"[QUsb2Snes] Removed item: {item_name} (address 0x{item_address:04X})")
        else:
            print(f"[QUsb2Snes] Failed to remove item: {item_name}")
        
        return success
    
    def add_item(self, item_name: str, value: int = 1) -> bool:
        """Add an item to inventory."""
        if item_name == 'blue_boomerang':
            return self._add_boomerang(blue=True)
        elif item_name == 'red_boomerang':
            return self._add_boomerang(red=True)
        elif item_name == 'mushroom':
            return self._add_mushroom_powder(mushroom=True)
        elif item_name == 'powder':
            return self._add_mushroom_powder(powder=True)
        elif item_name == 'shovel':
            return self._add_shovel_flute(shovel=True)
        elif item_name == 'flute':
            return self._add_shovel_flute(flute=True)
        elif item_name == 'silver_arrows':
            bow_addr = 0xF340
            arrows_addr = 0xF377
            self.write_memory(bow_addr, bytes([0x02]))
            arrows_data = self.read_memory(arrows_addr, size=1)
            if arrows_data and int(arrows_data[0]) == 0:
                self.write_memory(arrows_addr, bytes([30]))
            print(f"[QUsb2Snes] Added silver arrows (with bow)")
            return True
        
        item_address = None
        for addr, name in MEMORY_ADDRESSES.get('items_inventory', {}).items():
            if name == item_name:
                item_address = addr
                break
        
        if item_address is None:
            print(f"[QUsb2Snes] Item '{item_name}' not found in inventory addresses")
            return False
        
        success = self.write_memory(item_address, bytes([value]))
        if success:
            self.previous_values['items'][item_address] = value
            self.obtained_items.add(item_name)
            print(f"[QUsb2Snes] Added item: {item_name} = {value} (address 0x{item_address:04X})")
        else:
            print(f"[QUsb2Snes] Failed to add item: {item_name}")
        
        return success
    
    def check_deaths(self) -> Tuple[Optional[int], bool, bool]:
        """Check for death events using proper ALttP game mode and death counter."""
        game_mode_data = self.read_memory(MEMORY_ADDRESSES['game_mode'], size=1)
        game_mode = None
        if game_mode_data and len(game_mode_data) >= 1:
            game_mode = int(game_mode_data[0])
        
        death_count_data = self.read_memory(MEMORY_ADDRESSES['death_count'], size=1)
        death_count = None
        if death_count_data and len(death_count_data) >= 1:
            death_count = int(death_count_data[0])
        
        death_start_detected = False
        death_confirmed_detected = False
        
        if game_mode is not None:
            prev_game_mode = self.previous_values.get('game_mode')
            if prev_game_mode == 0x07 and game_mode == 0x09:
                death_start_detected = True
                if self.debug:
                    print(f"[DEBUG] 💀 Death start detected! Game mode: 0x{prev_game_mode:02X} -> 0x{game_mode:02X}")
            self.previous_values['game_mode'] = game_mode
        
        if death_count is not None:
            prev_death_count = self.previous_values.get('death_count')
            if prev_death_count is not None and death_count > prev_death_count:
                death_confirmed_detected = True
                if self.debug:
                    print(f"[DEBUG] 💀 Death confirmed! Counter: {prev_death_count} -> {death_count}")
            self.previous_values['death_count'] = death_count
        
        return (death_count, death_start_detected, death_confirmed_detected)
    
    def check_half_magic(self) -> bool:
        """Check for Half Magic acquisition."""
        half_magic_data = self.read_memory(MEMORY_ADDRESSES['half_magic'], size=1)
        if not half_magic_data or len(half_magic_data) < 1:
            return False
        
        current_value = int(half_magic_data[0])
        prev_value = self.previous_values.get('half_magic')
        
        half_magic_acquired = False
        if prev_value == 0x00 and current_value == 0x01:
            half_magic_acquired = True
            if self.debug:
                print(f"[DEBUG] ✨ Half Magic acquired! Value: 0x{prev_value:02X} -> 0x{current_value:02X}")
        
        self.previous_values['half_magic'] = current_value
        return half_magic_acquired
    
    def check_items(self) -> List[str]:
        """Check for new items obtained."""
        new_items = []
        
        for address, item_name in MEMORY_ADDRESSES.get('items_inventory', {}).items():
            data = self.read_memory(address, size=1)
            if data and len(data) >= 1:
                current_value = int(data[0])
                prev_value = self.previous_values['items'].get(address, 0)
                
                if self.debug and current_value != prev_value:
                    print(f"[DEBUG] Item {item_name} at 0x{address:04X}: {prev_value} -> {current_value}")
                
                if prev_value == 0 and current_value > 0:
                    new_items.append(item_name)
                
                self.previous_values['items'][address] = current_value
        
        actually_new = [item for item in new_items if item not in self.obtained_items]
        return actually_new

