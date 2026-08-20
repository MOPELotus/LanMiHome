plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "dev.lotus.lanmihome"
    compileSdk = 36

    defaultConfig {
        applicationId = "dev.lotus.lanmihome.client"
        minSdk = 26
        targetSdk = 36
        versionCode = 10
        versionName = "1.5.2"
    }

    flavorDimensions += "role"
    productFlavors {
        create("client") {
            dimension = "role"
            buildConfigField("boolean", "NIGHT_NODE_ENABLED", "false")
            manifestPlaceholders["nightNodeEnabled"] = "false"
        }
        create("night") {
            dimension = "role"
            buildConfigField("boolean", "NIGHT_NODE_ENABLED", "true")
            manifestPlaceholders["nightNodeEnabled"] = "true"
        }
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2026.06.00")
    implementation(composeBom)
    implementation("androidx.activity:activity-compose:1.13.0")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    implementation(platform("com.squareup.okhttp3:okhttp-bom:5.3.0"))
    implementation("com.squareup.okhttp3:okhttp")
    debugImplementation("androidx.compose.ui:ui-tooling")
}
