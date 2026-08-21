package dev.lotus.lanmihome

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.UUID
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeout

private const val W96D_PREF_ADDRESS = "w96d_ble_address"
private const val W96D_NAME_PREFIX = "W96D"

private val UUID_POWER = UUID.fromString("0000fff1-0000-1000-8000-00805f9b34fb")
private val UUID_FAN_SPEED = UUID.fromString("0000fff3-0000-1000-8000-00805f9b34fb")
private val UUID_NATURE_WIND = UUID.fromString("0000fff4-0000-1000-8000-00805f9b34fb")
private val UUID_SHUTDOWN_DELAY = UUID.fromString("0000fff5-0000-1000-8000-00805f9b34fb")
private val UUID_GEAR_DOWN = UUID.fromString("0000fff6-0000-1000-8000-00805f9b34fb")
private val UUID_SPEED_CALIB = UUID.fromString("0000fff7-0000-1000-8000-00805f9b34fb")
private val UUID_TURBO_TIME = UUID.fromString("0000fff8-0000-1000-8000-00805f9b34fb")
private val UUID_LIGHT = UUID.fromString("0000fffa-0000-1000-8000-00805f9b34fb")
private val UUID_TURBO_REMAINING = UUID.fromString("0000fffb-0000-1000-8000-00805f9b34fb")
private val UUID_TURBO = UUID.fromString("0000fffc-0000-1000-8000-00805f9b34fb")
private val UUID_BATTERY_INFO = UUID.fromString("0000ffd1-0000-1000-8000-00805f9b34fb")
private val UUID_POWER_STATUS = UUID.fromString("0000ffd2-0000-1000-8000-00805f9b34fb")
private val UUID_MOTOR_INFO = UUID.fromString("0000ffd3-0000-1000-8000-00805f9b34fb")

internal class W96DBleClient private constructor(context: Context) {
    private val app = context.applicationContext
    private val prefs = app.getSharedPreferences(MAIN_PREFS, Context.MODE_PRIVATE)
    private val adapter: BluetoothAdapter?
        get() = app.getSystemService(BluetoothManager::class.java)?.adapter

    private val connectMutex = Mutex()
    private val opMutex = Mutex()
    @Volatile private var gatt: BluetoothGatt? = null
    @Volatile private var deviceName: String? = null
    @Volatile private var address: String? = prefs.getString(W96D_PREF_ADDRESS, null)
    @Volatile private var lastError: String? = null

    private var readyWaiter: CompletableDeferred<Unit>? = null
    private var readWaiter: CompletableDeferred<Pair<UUID, ByteArray>>? = null
    private var writeWaiter: CompletableDeferred<UUID>? = null

    val isConnected: Boolean get() = gatt != null

    fun hasRuntimePermissions(): Boolean {
        return if (Build.VERSION.SDK_INT >= 31) {
            app.checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED &&
                app.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED
        } else {
            app.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
        }
    }

    @SuppressLint("MissingPermission")
    suspend fun connect(scanIfNeeded: Boolean): W96DState = connectMutex.withLock {
        if (!hasRuntimePermissions()) throw SecurityException("未授予蓝牙扫描/连接权限")
        gatt?.let { if (it.services.isNotEmpty()) return@withLock state() }
        closeGatt()

        val bt = adapter ?: throw IllegalStateException("设备没有可用蓝牙适配器")
        if (!bt.isEnabled) throw IllegalStateException("蓝牙未开启")

        var device = address?.let { saved -> runCatching { bt.getRemoteDevice(saved) }.getOrNull() }
        if (device != null) {
            try {
                connectDevice(device)
            } catch (e: Exception) {
                lastError = "保存设备直连失败：${e.message}"
                closeGatt()
                if (!scanIfNeeded) throw e
                device = null
            }
        }
        if (device == null) {
            if (!scanIfNeeded) throw W96DConnectionError("尚未保存 W96D 地址")
            device = scanForFan(bt)
            connectDevice(device)
        }

        address = device.address
        deviceName = runCatching { device.name }.getOrNull() ?: W96D_NAME_PREFIX
        prefs.edit().putString(W96D_PREF_ADDRESS, address).apply()
        lastError = null
        runCatching { write(UUID_SHUTDOWN_DELAY, be16(0)) }
        return@withLock state()
    }

    @SuppressLint("MissingPermission")
    private suspend fun scanForFan(bt: BluetoothAdapter): android.bluetooth.BluetoothDevice {
        val scanner = bt.bluetoothLeScanner ?: throw IllegalStateException("BLE 扫描器不可用")
        val found = CompletableDeferred<android.bluetooth.BluetoothDevice>()
        val callback = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: ScanResult) {
                val name = runCatching { result.device.name }.getOrNull() ?: result.scanRecord?.deviceName.orEmpty()
                if (name.startsWith(W96D_NAME_PREFIX, ignoreCase = true) && !found.isCompleted) found.complete(result.device)
            }
            override fun onScanFailed(errorCode: Int) {
                if (!found.isCompleted) found.completeExceptionally(IllegalStateException("BLE 扫描失败：$errorCode"))
            }
        }
        scanner.startScan(callback)
        return try {
            withTimeout(10_000) { found.await() }
        } finally {
            runCatching { scanner.stopScan(callback) }
        }
    }

    @SuppressLint("MissingPermission")
    private suspend fun connectDevice(device: android.bluetooth.BluetoothDevice) {
        val ready = CompletableDeferred<Unit>()
        readyWaiter = ready
        val localGatt = if (Build.VERSION.SDK_INT >= 23) {
            device.connectGatt(app, false, callback, android.bluetooth.BluetoothDevice.TRANSPORT_LE)
        } else {
            @Suppress("DEPRECATION")
            device.connectGatt(app, false, callback)
        }
        gatt = localGatt
        try {
            withTimeout(15_000) { ready.await() }
        } catch (e: Exception) {
            runCatching { localGatt.disconnect() }
            runCatching { localGatt.close() }
            if (gatt === localGatt) gatt = null
            throw e
        } finally {
            if (readyWaiter === ready) readyWaiter = null
        }
    }

    private val callback = object : BluetoothGattCallback() {
        @SuppressLint("MissingPermission")
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            if (status == BluetoothGatt.GATT_SUCCESS && newState == BluetoothProfile.STATE_CONNECTED) {
                if (!g.discoverServices()) readyWaiter?.completeExceptionally(IllegalStateException("无法启动 GATT 服务发现"))
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED || status != BluetoothGatt.GATT_SUCCESS) {
                val message = "GATT 断开 status=$status state=$newState"
                lastError = message
                readyWaiter?.takeIf { !it.isCompleted }?.completeExceptionally(W96DConnectionError(message))
                readWaiter?.takeIf { !it.isCompleted }?.completeExceptionally(W96DConnectionError(message))
                writeWaiter?.takeIf { !it.isCompleted }?.completeExceptionally(W96DConnectionError(message))
                if (gatt === g) {
                    runCatching { g.close() }
                    gatt = null
                }
            }
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            val waiter = readyWaiter ?: return
            if (status == BluetoothGatt.GATT_SUCCESS) waiter.complete(Unit)
            else waiter.completeExceptionally(W96DConnectionError("GATT 服务发现失败：$status"))
        }

        @Deprecated("legacy callback")
        override fun onCharacteristicRead(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) {
            completeRead(c.uuid, c.value ?: byteArrayOf(), status)
        }

        override fun onCharacteristicRead(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray, status: Int) {
            completeRead(c.uuid, value, status)
        }

        override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) {
            val waiter = writeWaiter ?: return
            if (status == BluetoothGatt.GATT_SUCCESS) waiter.complete(c.uuid)
            else waiter.completeExceptionally(W96DConnectionError("GATT 写入 ${c.uuid} 失败：$status"))
        }
    }

    private fun completeRead(uuid: UUID, value: ByteArray, status: Int) {
        val waiter = readWaiter ?: return
        if (status == BluetoothGatt.GATT_SUCCESS) waiter.complete(uuid to value.copyOf())
        else waiter.completeExceptionally(W96DConnectionError("GATT 读取 $uuid 失败：$status"))
    }

    @SuppressLint("MissingPermission")
    suspend fun disconnect() = connectMutex.withLock { closeGatt() }

    @SuppressLint("MissingPermission")
    private fun closeGatt() {
        val old = gatt
        gatt = null
        if (old != null) {
            runCatching { old.disconnect() }
            runCatching { old.close() }
        }
        readWaiter = null
        writeWaiter = null
        readyWaiter = null
    }

    private fun characteristic(uuid: UUID): BluetoothGattCharacteristic {
        val gg = gatt ?: throw W96DConnectionError("W96D 蓝牙未连接")
        return gg.services.asSequence().mapNotNull { it.getCharacteristic(uuid) }.firstOrNull()
            ?: throw W96DConnectionError("W96D 缺少 GATT 特征 $uuid")
    }

    @SuppressLint("MissingPermission")
    private suspend fun read(uuid: UUID): ByteArray = opMutex.withLock {
        val gg = gatt ?: throw W96DConnectionError("W96D 蓝牙未连接")
        val ch = characteristic(uuid)
        val waiter = CompletableDeferred<Pair<UUID, ByteArray>>()
        readWaiter = waiter
        try {
            if (!gg.readCharacteristic(ch)) throw W96DConnectionError("无法发起 GATT 读取 $uuid")
            val (actual, data) = withTimeout(4_000) { waiter.await() }
            if (actual != uuid) throw W96DConnectionError("GATT 读取响应错位：$actual")
            data
        } finally {
            if (readWaiter === waiter) readWaiter = null
        }
    }

    private suspend fun readOptional(uuid: UUID): ByteArray? = runCatching { read(uuid) }.getOrNull()

    @SuppressLint("MissingPermission")
    private suspend fun write(uuid: UUID, value: ByteArray, response: Boolean = true) = opMutex.withLock {
        val gg = gatt ?: throw W96DConnectionError("W96D 蓝牙未连接")
        val ch = characteristic(uuid)
        val type = if (response) BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT else BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
        if (!response) {
            val accepted = if (Build.VERSION.SDK_INT >= 33) {
                gg.writeCharacteristic(ch, value, type) == BluetoothGatt.GATT_SUCCESS
            } else {
                @Suppress("DEPRECATION")
                run { ch.writeType = type; ch.value = value; gg.writeCharacteristic(ch) }
            }
            if (!accepted) throw W96DConnectionError("无法发起 GATT 写入 $uuid")
            delay(60)
            return@withLock
        }

        val waiter = CompletableDeferred<UUID>()
        writeWaiter = waiter
        try {
            val accepted = if (Build.VERSION.SDK_INT >= 33) {
                gg.writeCharacteristic(ch, value, type) == BluetoothGatt.GATT_SUCCESS
            } else {
                @Suppress("DEPRECATION")
                run { ch.writeType = type; ch.value = value; gg.writeCharacteristic(ch) }
            }
            if (!accepted) throw W96DConnectionError("无法发起 GATT 写入 $uuid")
            val actual = withTimeout(4_000) { waiter.await() }
            if (actual != uuid) throw W96DConnectionError("GATT 写入响应错位：$actual")
        } finally {
            if (writeWaiter === waiter) writeWaiter = null
        }
    }

    suspend fun state(): W96DState {
        if (gatt == null) return W96DState(false, connected = false, source = "phone-ble", error = lastError, name = deviceName, address = address)
        return try {
            // Android GATT is effectively single-flight; keep all reads sequential.
            val powerRaw = readOptional(UUID_POWER)
            val speedRaw = readOptional(UUID_FAN_SPEED)
            val natureRaw = readOptional(UUID_NATURE_WIND)
            val turboRaw = readOptional(UUID_TURBO)
            val turboRemainRaw = readOptional(UUID_TURBO_REMAINING)
            val lightRaw = readOptional(UUID_LIGHT)
            val shutdownRaw = readOptional(UUID_SHUTDOWN_DELAY)
            val speedsRaw = readOptional(UUID_SPEED_CALIB)
            val batteryRaw = readOptional(UUID_BATTERY_INFO)
            val pwrRaw = readOptional(UUID_POWER_STATUS)
            val motorRaw = readOptional(UUID_MOTOR_INFO)

            val gear = powerRaw?.firstOrNull()?.toInt()?.and(0xff)
            val turboRemain = turboRemainRaw?.takeIf { it.size >= 2 }?.let { u16be(it, 0) }
            var state = W96DState(
                available = true, connected = true, source = "phone-ble", error = null,
                name = deviceName ?: W96D_NAME_PREFIX, address = address,
                power = gear?.let { it != 0 }, gear = gear,
                speed = speedRaw?.firstOrNull()?.toInt()?.and(0xff),
                naturalWind = natureRaw?.firstOrNull()?.let { it.toInt() != 0 },
                turbo = turboRaw?.firstOrNull()?.let { it.toInt() != 0 } ?: turboRemain?.let { it > 0 },
                turboRemainingSeconds = turboRemain,
                light = lightRaw?.firstOrNull()?.let { it.toInt() != 0 },
                shutdownDelaySeconds = shutdownRaw?.takeIf { it.size >= 2 }?.let { u16be(it, 0) },
                gearSpeeds = speedsRaw?.takeIf { it.size >= 4 }?.take(4)?.map { it.toInt() and 0xff },
            )
            if (batteryRaw != null && batteryRaw.size >= 8) {
                state = state.copy(
                    batteryVoltage = u16be(batteryRaw, 0) / 1000.0,
                    batteryCurrentMa = i16be(batteryRaw, 2),
                    batteryCapacityMwh = u32be(batteryRaw, 4),
                )
            }
            if (pwrRaw != null && pwrRaw.size >= 8) {
                var current = i16be(pwrRaw, 4)
                if (current == 0x7fff) current = 0
                state = state.copy(
                    vbusVoltage = u32be(pwrRaw, 0) / 1000.0,
                    vbusCurrentMa = current,
                    chargeStatus = pwrRaw[7].toInt() and 0xff,
                )
            }
            if (motorRaw != null && motorRaw.size >= 3) {
                val current = u16be(motorRaw, 0)
                val voltage = u16be(motorRaw, motorRaw.size - 2)
                state = state.copy(
                    motorCurrentMa = current,
                    motorVoltage = voltage / 1000.0,
                    motorPowerW = (current / 1000.0) * (voltage / 1000.0),
                )
            }
            lastError = null
            state
        } catch (e: Exception) {
            lastError = "${e.javaClass.simpleName}: ${e.message}"
            W96DState(false, connected = gatt != null, source = "phone-ble", error = lastError, name = deviceName, address = address)
        }
    }

    suspend fun patch(values: Map<String, Any>): W96DState {
        if (gatt == null) connect(scanIfNeeded = false)
        val unknown = values.keys - setOf("power", "speed", "natural_wind", "turbo", "light", "shutdown_delay_seconds", "gear_down_mode", "gear_speeds", "turbo_time_seconds")
        require(unknown.isEmpty()) { "未知 W96D 字段：${unknown.joinToString()}" }

        values["power"]?.let { write(UUID_POWER, byteArrayOf(if (asBool(it, "power")) 1 else 0)) }
        values["speed"]?.let {
            val speed = asInt(it, "speed", 0, 100)
            write(UUID_TURBO, byteArrayOf(0)); write(UUID_NATURE_WIND, byteArrayOf(0))
            val current = readOptional(UUID_POWER)
            if (current == null || current.firstOrNull()?.toInt() == 0) write(UUID_POWER, byteArrayOf(1))
            write(UUID_FAN_SPEED, byteArrayOf(speed.toByte()), response = false)
        }
        values["natural_wind"]?.let {
            val on = asBool(it, "natural_wind")
            if (on) write(UUID_TURBO, byteArrayOf(0))
            write(UUID_NATURE_WIND, byteArrayOf(if (on) 1 else 0))
        }
        values["turbo"]?.let {
            val on = asBool(it, "turbo")
            if (on) write(UUID_NATURE_WIND, byteArrayOf(0))
            write(UUID_TURBO, byteArrayOf(if (on) 1 else 0))
        }
        values["light"]?.let { write(UUID_LIGHT, byteArrayOf(if (asBool(it, "light")) 1 else 0)) }
        values["shutdown_delay_seconds"]?.let { write(UUID_SHUTDOWN_DELAY, be16(asInt(it, "shutdown_delay_seconds", 0, 65535))) }
        values["gear_down_mode"]?.let { write(UUID_GEAR_DOWN, byteArrayOf(asInt(it, "gear_down_mode", 0, 1).toByte())) }
        values["turbo_time_seconds"]?.let { write(UUID_TURBO_TIME, be16(asInt(it, "turbo_time_seconds", 0, 600))) }
        values["gear_speeds"]?.let { raw ->
            require(raw is List<*>) { "gear_speeds 必须是四个数值" }
            require(raw.size == 4) { "gear_speeds 必须是四个数值" }
            write(UUID_SPEED_CALIB, raw.map { asInt(it ?: 0, "gear_speeds", 0, 100).toByte() }.toByteArray())
        }
        return state()
    }

    private fun asBool(value: Any, field: String): Boolean = when (value) {
        is Boolean -> value
        is Number -> when (value.toInt()) { 0 -> false; 1 -> true; else -> throw IllegalArgumentException("$field 必须是布尔值") }
        else -> throw IllegalArgumentException("$field 必须是布尔值")
    }

    private fun asInt(value: Any, field: String, min: Int, max: Int): Int {
        val n = (value as? Number)?.toInt() ?: value.toString().toIntOrNull() ?: throw IllegalArgumentException("$field 必须是整数")
        require(n in min..max) { "$field 必须在 $min..$max" }
        return n
    }

    private fun be16(value: Int) = byteArrayOf((value ushr 8).toByte(), value.toByte())
    private fun u16be(data: ByteArray, offset: Int) = ByteBuffer.wrap(data, offset, 2).order(ByteOrder.BIG_ENDIAN).short.toInt() and 0xffff
    private fun i16be(data: ByteArray, offset: Int) = ByteBuffer.wrap(data, offset, 2).order(ByteOrder.BIG_ENDIAN).short.toInt()
    private fun u32be(data: ByteArray, offset: Int) = ByteBuffer.wrap(data, offset, 4).order(ByteOrder.BIG_ENDIAN).int.toLong() and 0xffffffffL

    companion object {
        @Volatile private var instance: W96DBleClient? = null
        fun get(context: Context): W96DBleClient = instance ?: synchronized(this) {
            instance ?: W96DBleClient(context).also { instance = it }
        }
    }
}

private class W96DConnectionError(message: String) : IllegalStateException(message)
