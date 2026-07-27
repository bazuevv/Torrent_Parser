package com.tonapps.wallet.data.banner.source

import com.google.firebase.crashlytics.FirebaseCrashlytics
import com.tonapps.blockchain.ton.TonNetwork
import com.tonapps.wallet.api.API
import com.tonapps.wallet.data.banner.entities.BannerDataEntity
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

internal class RemoteDataSource(
    private val api: API
) {

    suspend fun load(network: TonNetwork): BannerDataEntity? = withContext(Dispatchers.IO) {
        try {
            BannerDataEntity(api.getBanners(network))
        } catch (e: Throwable) {
            FirebaseCrashlytics.getInstance().recordException(e)
            null
        }
    }
}
