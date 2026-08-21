package dev.lotus.lanmihome

/**
 * Android Night Node transport abstraction for W96D.
 *
 * The scheduler decides ownership. This class only provides the BLE side
 * contract and never competes with router or phone ownership.
 */
class W96DNightNode {
    var connected: Boolean = false
        private set

    fun connect(): Boolean {
        // BluetoothGatt implementation is provided by the Android BLE layer.
        connected = true
        return connected
    }

    fun disconnect() {
        connected = false
    }

    fun owner(): String = "night_node"
}
