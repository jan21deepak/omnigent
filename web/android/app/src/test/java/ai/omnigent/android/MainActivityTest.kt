package ai.omnigent.android

import android.content.res.Configuration
import android.view.View
import android.view.ViewGroup
import android.webkit.WebView
import androidx.core.view.WindowInsetsControllerCompat
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class MainActivityTest {
    @Test
    fun `webview leaves algorithmic darkening disabled`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        assertFalse(activity.webView().settings.isAlgorithmicDarkeningAllowed)
    }

    @Test
    @Config(qualifiers = "notnight")
    fun `light configuration uses dark status bar icons`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        assertTrue(insetsController.isAppearanceLightStatusBars)
        assertTrue(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    @Config(qualifiers = "night")
    fun `dark configuration uses light status bar icons`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        assertFalse(insetsController.isAppearanceLightStatusBars)
        assertFalse(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    fun `configuration change updates system bar icon polarity`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        val darkConfiguration =
            Configuration(activity.resources.configuration).apply {
                uiMode =
                    (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or
                    Configuration.UI_MODE_NIGHT_YES
            }
        activity.onConfigurationChanged(darkConfiguration)
        assertFalse(insetsController.isAppearanceLightStatusBars)
        assertFalse(insetsController.isAppearanceLightNavigationBars)

        val lightConfiguration =
            Configuration(activity.resources.configuration).apply {
                uiMode =
                    (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or
                    Configuration.UI_MODE_NIGHT_NO
            }
        activity.onConfigurationChanged(lightConfiguration)
        assertTrue(insetsController.isAppearanceLightStatusBars)
        assertTrue(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    fun `density change rescales the server-switcher pill metrics`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val pill = activity.switchButton()

        val startDp = activity.resources.displayMetrics.density
        assertEquals((12 * startDp).toInt(), pill.paddingLeft)
        assertEquals(6 * startDp, pill.elevation, 0.001f)

        RuntimeEnvironment.setQualifiers("+560dpi")
        val denserConfig = Configuration(activity.resources.configuration)
        activity.onConfigurationChanged(denserConfig)

        val newDp = activity.resources.displayMetrics.density
        assertTrue("density should have increased", newDp > startDp)
        assertEquals((12 * newDp).toInt(), pill.paddingLeft)
        assertEquals((6 * newDp).toInt(), pill.paddingTop)
        assertEquals(6 * newDp, pill.elevation, 0.001f)
    }

    @Test
    fun `server switcher pill gets a 48dp tall touch target`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val pill = activity.switchButton()
        val container = pill.parent as ViewGroup
        val density = activity.resources.displayMetrics.density

        pill.layout(100, 40, 220, 40 + (27 * density).toInt())

        val delegate = shadowOf(container.touchDelegate)
        assertEquals(pill, delegate.delegateView)
        assertEquals(pill.left, delegate.bounds.left)
        assertEquals(pill.top, delegate.bounds.top)
        assertEquals((48 * density).toInt(), delegate.bounds.height())
    }

    @Test
    fun `touch target rescales after a density change`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val pill = activity.switchButton()
        val container = pill.parent as ViewGroup

        RuntimeEnvironment.setQualifiers("+560dpi")
        activity.onConfigurationChanged(Configuration(activity.resources.configuration))
        val newDensity = activity.resources.displayMetrics.density

        pill.layout(100, 40, 220, 40 + (27 * newDensity).toInt())

        val delegate = shadowOf(container.touchDelegate)
        assertEquals((48 * newDensity).toInt(), delegate.bounds.height())
    }

    private fun MainActivity.switchButton(): View =
        MainActivity::class
            .java
            .getDeclaredField("switchButton")
            .apply { isAccessible = true }
            .get(this) as View

    private fun MainActivity.webView(): WebView =
        MainActivity::class
            .java
            .getDeclaredField("webView")
            .apply { isAccessible = true }
            .get(this) as WebView
}
