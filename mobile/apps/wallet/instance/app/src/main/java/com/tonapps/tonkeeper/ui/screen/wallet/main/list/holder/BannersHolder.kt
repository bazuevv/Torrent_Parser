package com.tonapps.tonkeeper.ui.screen.wallet.main.list.holder

import android.view.ViewGroup
import androidx.compose.ui.platform.ComposeView
import androidx.compose.ui.platform.ViewCompositionStrategy
import com.tonapps.core.components.BannersViewer
import com.tonapps.tonkeeper.Environment
import com.tonapps.tonkeeper.ui.screen.wallet.main.list.Item
import com.tonapps.tonkeeperx.R
import com.tonapps.wallet.data.banner.BannerRepository
import org.koin.core.component.KoinComponent
import org.koin.core.component.inject
import ui.theme.MoonTheme

class BannersHolder(parent: ViewGroup): Holder<Item.Banners>(parent, R.layout.view_wallet_banner), KoinComponent {

    private val environment: Environment by inject()
    private val bannerRepository: BannerRepository by inject()

    private val composeView = findViewById<ComposeView>(R.id.compose_view).apply {
        setViewCompositionStrategy(ViewCompositionStrategy.DisposeOnDetachedFromWindowOrReleasedFromPool)
    }

    override fun onBind(item: Item.Banners) {
        composeView.setContent {
            MoonTheme(colorScheme = environment.theme) {
                BannersViewer(
                    banners = item.banners,
                    onClick = { button ->
                        navigation?.openURL(button.payload)
                    },
                    onHide = { banner ->
                        bannerRepository.hideBanner(item.walletId, banner.id)
                    }
                )
            }
        }
    }
}
