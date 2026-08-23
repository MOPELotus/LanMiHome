package dev.lotus.lanmihome

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.material3.MaterialTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (BuildConfig.NIGHT_NODE_ENABLED) W96dNightService.start(this)
        setContent { MaterialTheme { LanMiHomeApp() } }
    }
}
