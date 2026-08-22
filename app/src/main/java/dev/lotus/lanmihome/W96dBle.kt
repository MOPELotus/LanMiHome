package dev.lotus.lanmihome

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattService
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import java.time.OffsetDateTime
import java.util.UUID
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeout

internal object W96dGattUuids {
    fun uuid16(value: Int): UUID = UUID.fromString("0000${value.toString(16).padStart(4, '0')}-0000-1000-8000-00805f9b34fb")
    val MAIN_SERVICE = uuid16(0xFFF0)
    val POWER_SERVICE = uuid16(0xFFD0)
    val POWER = uuid16(0xFFF1)
    val SPEED = uuid16(0xFFF3)
    val NATURAL = uuid16(0xFFF4)
    val SHUTDOWN_DELAY = uuid16(0xFFF5)
    val GEAR_DOWN_MODE = uuid16(0xFFF6)
    val SPEED_CALIB = uuid16(0xFFF7)
    val TURBO_TIME = uuid16(0xFFF8)
    val LIGHT = uuid16(0xFFFA)
    val TURBO_REMAINING = uuid16(0xFFFB)
    val TURBO = uuid16(0xFFFC)
    val BATTERY = uuid16(0xFFD1)
    val POWER_STATUS = uuid16(0xFFD2)
    val MOTOR = uuid16(0xFFD3)
}

internal fun requiredW96dBlePermissions(): Array<String> = if (Build.VERSION.SDK_INT >= 31) {
    arrayOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
} else {
    arrayOf(Manifest.permission.ACCESS_FINE_LOCATION)
}

internal fun hasW96dBlePermissions(context: Context): Boolean =
    requiredW96dBlePermissions().all { context.checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED }

private data class PendingRead(val uuid: UUID, val deferred: CompletableDeferred<ByteArray>)
private data class PendingWrite(val uuid: UUID, val deferred: CompletableDeferred<Unit>)

internal class W96dGattClient(private val context: Context) {
    private val manager = context.getSystemService(BluetoothManager::class.java)
        ?: throw IllegalStateException("BluetoothManager unavailable")
    private val adapter get() = manager.adapter ?: throw IllegalStateException("Bluetooth unavailable")
    private val opMutex = Mutex()
    private val connectionMutex = Mutex()
    @Volatile private var gatt: BluetoothGatt? = null
    @Volatile private var connectDeferred: CompletableDeferred<Unit>? = null
    @Volatile private var pendingRead: PendingRead? = null
    @Volatile private var pendingWrite: PendingWrite? = null
    @Volatile private var lastState = W96dState(owner = "phone")

    private val callback = object : BluetoothGattCallback() {
        @SuppressLint("MissingPermission")
        override fun onConnectionStateChange(callbackGatt: BluetoothGatt, status: Int, newState: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                connectDeferred?.completeExceptionally(IllegalStateException("GATT connect status=$status"))
                if (gatt === callbackGatt) gatt = null
                markDisconnected("GATT status=$status")
                runCatching { callbackGatt.close() }
                return
            }
            when (newState) {
                BluetoothProfile.STATE_CONNECTED -> {
                    if (!callbackGatt.discoverServices()) {
                        connectDeferred?.completeExceptionally(IllegalStateException("discoverServices returned false"))
                    }
                }
                BluetoothProfile.STATE_DISCONNECTED -> {
                    connectDeferred?.completeExceptionally(IllegalStateException("W96D disconnected"))
                    if (gatt === callbackGatt) gatt = null
                    markDisconnected("disconnected")
                    runCatching { callbackGatt.close() }
                }
            }
        }

        override fun onServicesDiscovered(callbackGatt: BluetoothGatt, status: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                connectDeferred?.completeExceptionally(IllegalStateException("service discovery status=$status"))
                return
            }
            val main = callbackGatt.getService(W96dGattUuids.MAIN_SERVICE)
            if (main == null) {
                connectDeferred?.completeExceptionally(IllegalStateException("FFF0 service not found"))
                return
            }
            val required = listOf(
                W96dGattUuids.POWER,
                W96dGattUuids.SPEED,
                W96dGattUuids.NATURAL,
                W96dGattUuids.LIGHT,
                W96dGattUuids.TURBO,
                W96dGattUuids.TURBO_REMAINING,
            )
            val missing = required.filter { main.getCharacteristic(it) == null }
            if (missing.isNotEmpty()) {
                connectDeferred?.completeExceptionally(IllegalStateException("missing W96D characteristics: $missing"))
                return
            }
            if (callbackGatt.getService(W96dGattUuids.POWER_SERVICE) == null) {
                connectDeferred?.completeExceptionally(IllegalStateException("FFD0 telemetry service not found"))
                return
            }
            connectDeferred?.complete(Unit)
        }

        @Deprecated("Android 13 callback kept for API 26-32")
        override fun onCharacteristicRead(gatt: BluetoothGatt, characteristic: BluetoothGattCharacteristic, status: Int) {
            finishRead(characteristic.uuid, characteristic.value?.clone() ?: byteArrayOf(), status)
        }

        override fun onCharacteristicRead(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            value: ByteArray,
            status: Int,
        ) {
            finishRead(characteristic.uuid, value.clone(), status)
        }

        override fun onCharacteristicWrite(gatt: BluetoothGatt, characteristic: BluetoothGattCharacteristic, status: Int) {
            val pending = pendingWrite
            if (pending == null || pending.uuid != characteristic.uuid) return
            pendingWrite = null
            if (status == BluetoothGatt.GATT_SUCCESS) pending.deferred.complete(Unit)
            else pending.deferred.completeExceptionally(IllegalStateException("GATT write ${characteristic.uuid} status=$status"))
        }
    }

    private fun finishRead(uuid: UUID, value: ByteArray, status: Int) {
        val pending = pendingRead
        if (pending == null || pending.uuid != uuid) return
        pendingRead = null
        if (status == BluetoothGatt.GATT_SUCCESS) pending.deferred.complete(value)
        else pending.deferred.completeExceptionally(IllegalStateException("GATT read $uuid status=$status"))
    }

    @SuppressLint("MissingPermission")
    suspend fun connect(): W96dState = connectionMutex.withLock {
        require(hasW96dBlePermissions(context)) { "缺少蓝牙权限" }
        val existing = gatt
        if (existing != null && existing.services.isNotEmpty()) return@withLock refreshState()
        disconnectInternal()
        val savedAddress = W96dPrefs.bleAddress(context)
        val device = if (savedAddress.isNotBlank()) {
            runCatching { adapter.getRemoteDevice(savedAddress) }.getOrNull() ?: scanDevice()
        } else scanDevice()
        W96dPrefs.setBleAddress(context, device.address)
        val deferred = CompletableDeferred<Unit>()
        connectDeferred = deferred
        val localGatt = device.connectGatt(context, false, callback, BluetoothDevice.TRANSPORT_LE)
        gatt = localGatt
        try {
            withTimeout(15_000) { deferred.await() }
        } catch (e: Exception) {
            W96dPrefs.setBleAddress(context, "")
            disconnectInternal()
            throw e
        } finally {
            connectDeferred = null
        }
        lastState = lastState.copy(connected = true, available = true, address = device.address, error = null)
        refreshState()
    }

    @SuppressLint("MissingPermission")
    private suspend fun scanDevice(): BluetoothDevice {
        require(adapter.isEnabled) { "蓝牙未开启" }
        val scanner = adapter.bluetoothLeScanner ?: throw IllegalStateException("BLE scanner unavailable")
        val found = CompletableDeferred<BluetoothDevice>()
        val cb = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: ScanResult) {
                val name = result.scanRecord?.deviceName ?: runCatching { result.device.name }.getOrNull().orEmpty()
                if (name.contains("W96D", ignoreCase = true) && !found.isCompleted) found.complete(result.device)
            }
            override fun onScanFailed(errorCode: Int) {
                if (!found.isCompleted) found.completeExceptionally(IllegalStateException("BLE scan failed=$errorCode"))
            }
        }
        val settings = ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build()
        scanner.startScan(null, settings, cb)
        return try {
            withTimeout(10_000) { found.await() }
        } finally {
            runCatching { scanner.stopScan(cb) }
        }
    }

    @SuppressLint("MissingPermission")
    suspend fun disconnect() = connectionMutex.withLock { disconnectInternal() }

    @SuppressLint("MissingPermission")
    private fun disconnectInternal() {
        val local = gatt
        gatt = null
        pendingRead?.deferred?.cancel(); pendingRead = null
        pendingWrite?.deferred?.cancel(); pendingWrite = null
        if (local != null) {
            runCatching { local.disconnect() }
            runCatching { local.close() }
        }
        markDisconnected(null)
    }

    private fun markDisconnected(error: String?) {
        lastState = lastState.copy(available = false, connected = false, error = error)
    }

    suspend fun state(): W96dState = if (gatt != null) refreshState() else lastState

    suspend fun patch(key: String, value: Any): W96dState = opMutex.withLock {
        ensureConnected()
        when (key) {
            "power" -> setPowerLocked(value as Boolean)
            "speed" -> setSpeedLocked((value as Number).toInt())
            "natural" -> setNaturalLocked(value as Boolean)
            "turbo" -> setTurboLocked(value as Boolean)
            "indicator" -> write(W96dGattUuids.LIGHT, byteArrayOf(if (value as Boolean) 1 else 0))
            else -> throw IllegalArgumentException("unknown W96D field: $key")
        }
        delay(80)
        refreshStateLocked()
    }

    private suspend fun setPowerLocked(on: Boolean) {
        write(W96dGattUuids.TURBO, byteArrayOf(0))
        write(W96dGattUuids.NATURAL, byteArrayOf(0))
        write(W96dGattUuids.POWER, byteArrayOf(if (on) 1 else 0))
    }

    private suspend fun setSpeedLocked(speed: Int) {
        require(speed in 0..100) { "speed must be 0..100" }
        write(W96dGattUuids.TURBO, byteArrayOf(0))
        write(W96dGattUuids.POWER, byteArrayOf(1))
        write(W96dGattUuids.NATURAL, byteArrayOf(0))
        write(W96dGattUuids.SPEED, byteArrayOf(speed.toByte()), preferNoResponse = true)
    }

    private suspend fun setNaturalLocked(on: Boolean) {
        if (on) {
            write(W96dGattUuids.TURBO, byteArrayOf(0))
            delay(100)
        }
        write(W96dGattUuids.NATURAL, byteArrayOf(if (on) 1 else 0))
    }

    private suspend fun setTurboLocked(on: Boolean) {
        if (on) {
            write(W96dGattUuids.NATURAL, byteArrayOf(0))
            delay(100)
        }
        write(W96dGattUuids.TURBO, byteArrayOf(if (on) 1 else 0))
    }

    suspend fun refreshState(): W96dState = opMutex.withLock { refreshStateLocked() }

    private suspend fun refreshStateLocked(): W96dState {
        ensureConnected()
        val power = read(W96dGattUuids.POWER)
        val speed = read(W96dGattUuids.SPEED)
        val natural = read(W96dGattUuids.NATURAL)
        val light = read(W96dGattUuids.LIGHT)
        val turboRemaining = read(W96dGattUuids.TURBO_REMAINING)
        val battery = read(W96dGattUuids.BATTERY)
        val powerStatus = read(W96dGattUuids.POWER_STATUS)
        val motor = read(W96dGattUuids.MOTOR)
        val turboSeconds = turboRemaining.beU16OrNull(0)
        lastState = W96dState(
            available = true,
            connected = true,
            name = "W96D",
            address = gatt?.device?.address,
            owner = "phone",
            scheduled = true,
            paused = false,
            power = power.firstOrNull()?.toInt()?.and(0xff)?.let { it != 0 },
            speed = speed.firstOrNull()?.toInt()?.and(0xff),
            natural = natural.firstOrNull()?.toInt()?.and(0xff)?.let { it != 0 },
            turbo = turboSeconds?.let { it > 0 },
            turboRemainingSeconds = turboSeconds,
            indicator = light.firstOrNull()?.toInt()?.and(0xff)?.let { it != 0 },
            batteryVoltageMv = battery.beU16OrNull(0),
            batteryCurrentMa = battery.beS16OrNull(2),
            batteryCapacityMwh = battery.beU32OrNull(4),
            vbusVoltageMv = powerStatus.beU32OrNull(0),
            vbusCurrentMa = powerStatus.beS16OrNull(4)?.let { if (it == 0x7fff) 0 else it },
            chargeStatus = powerStatus.getOrNull(7)?.toInt()?.and(0xff),
            motorCurrentMa = motor.beU16OrNull(0),
            motorVoltageMv = if (motor.size >= 2) motor.beU16OrNull(motor.size - 2) else null,
            motorBlocked = motor.getOrNull(2)?.toInt()?.and(0xff)?.let { it != 0 },
            error = null,
            updatedAt = OffsetDateTime.now().toString(),
        )
        return lastState
    }

    private fun ensureConnected() {
        val local = gatt ?: throw IllegalStateException("W96D 未连接")
        if (local.services.isEmpty()) throw IllegalStateException("W96D GATT 服务未就绪")
    }

    private fun characteristic(uuid: UUID): BluetoothGattCharacteristic {
        val local = gatt ?: throw IllegalStateException("W96D 未连接")
        for (service: BluetoothGattService in local.services) {
            service.getCharacteristic(uuid)?.let { return it }
        }
        throw IllegalStateException("W96D characteristic missing: $uuid")
    }

    @SuppressLint("MissingPermission")
    private suspend fun read(uuid: UUID): ByteArray {
        val local = gatt ?: throw IllegalStateException("W96D 未连接")
        val ch = characteristic(uuid)
        val deferred = CompletableDeferred<ByteArray>()
        pendingRead = PendingRead(uuid, deferred)
        if (!local.readCharacteristic(ch)) {
            pendingRead = null
            throw IllegalStateException("readCharacteristic returned false: $uuid")
        }
        return try {
            withTimeout(5_000) { deferred.await() }
        } finally {
            if (pendingRead?.deferred === deferred) pendingRead = null
        }
    }

    @SuppressLint("MissingPermission")
    private suspend fun write(uuid: UUID, value: ByteArray, preferNoResponse: Boolean = false) {
        val local = gatt ?: throw IllegalStateException("W96D 未连接")
        val ch = characteristic(uuid)
        val canWrite = ch.properties and BluetoothGattCharacteristic.PROPERTY_WRITE != 0
        val canNoResponse = ch.properties and BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE != 0
        val noResponse = (preferNoResponse && canNoResponse) || (!canWrite && canNoResponse)
        if (!canWrite && !canNoResponse) throw IllegalStateException("characteristic is not writable: $uuid")

        if (noResponse) {
            val started = if (Build.VERSION.SDK_INT >= 33) {
                local.writeCharacteristic(ch, value, BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE) == BluetoothGatt.GATT_SUCCESS
            } else {
                @Suppress("DEPRECATION")
                run {
                    ch.writeType = BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
                    ch.value = value
                    local.writeCharacteristic(ch)
                }
            }
            if (!started) throw IllegalStateException("writeCharacteristic returned false: $uuid")
            delay(60)
            return
        }

        val deferred = CompletableDeferred<Unit>()
        pendingWrite = PendingWrite(uuid, deferred)
        val started = if (Build.VERSION.SDK_INT >= 33) {
            local.writeCharacteristic(ch, value, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT) == BluetoothGatt.GATT_SUCCESS
        } else {
            @Suppress("DEPRECATION")
            run {
                ch.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                ch.value = value
                local.writeCharacteristic(ch)
            }
        }
        if (!started) {
            pendingWrite = null
            throw IllegalStateException("writeCharacteristic returned false: $uuid")
        }
        try {
            withTimeout(5_000) { deferred.await() }
        } finally {
            if (pendingWrite?.deferred === deferred) pendingWrite = null
        }
    }
}

private fun ByteArray.beU16OrNull(offset: Int): Int? {
    if (size < offset + 2) return null
    return ((this[offset].toInt() and 0xff) shl 8) or (this[offset + 1].toInt() and 0xff)
}

private fun ByteArray.beS16OrNull(offset: Int): Int? {
    val u = beU16OrNull(offset) ?: return null
    return if (u and 0x8000 != 0) u - 0x10000 else u
}

private fun ByteArray.beU32OrNull(offset: Int): Long? {
    if (size < offset + 4) return null
    return ((this[offset].toLong() and 0xff) shl 24) or
        ((this[offset + 1].toLong() and 0xff) shl 16) or
        ((this[offset + 2].toLong() and 0xff) shl 8) or
        (this[offset + 3].toLong() and 0xff)
}
