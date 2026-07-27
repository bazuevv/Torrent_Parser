package com.tonapps.wallet.data.banner

import org.koin.core.module.dsl.singleOf
import org.koin.dsl.module

val bannerModule = module {
    singleOf(::BannerRepository)
}
