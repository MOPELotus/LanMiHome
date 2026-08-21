package dev.lotus.lanmihome

/**
 * BLE transport abstraction for W96D.
 *
 * Implementations:
 * - router BlueZ sidecar
 * - 10S Night Node BluetoothGatt
 * - phone outdoor BluetoothGatt
 */
interface W96DBleTransport {
    val owner: String

    suspend fun connect()
    suspend fun disconnect()
    suspend fun write(characteristic: String, value: ByteArray)
    suspend fun read(characteristic: String): ByteArray?
}
