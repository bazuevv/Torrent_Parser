package com.tonapps.wallet.data.banner

import android.content.Context
import com.tonapps.blockchain.ton.TonNetwork
import com.tonapps.wallet.api.API
import com.tonapps.wallet.api.entity.BannerEntity
import com.tonapps.wallet.data.banner.entities.BannerDataEntity
import com.tonapps.wallet.data.banner.source.HiddenBannersDataSource
import com.tonapps.wallet.data.banner.source.LocalDataSource
import com.tonapps.wallet.data.banner.source.RemoteDataSource
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

class BannerRepository(context: Context, api: API) {

    private val localDataSource: LocalDataSource by lazy(LazyThreadSafetyMode.SYNCHRONIZED) {
        LocalDataSource(context)
    }

    private val hiddenDataSource: HiddenBannersDataSource by lazy(LazyThreadSafetyMode.SYNCHRONIZED) {
        HiddenBannersDataSource(context)
    }

    private val remoteDataSource = RemoteDataSource(api)

    suspend fun getBanners(
        walletId: String,
        network: TonNetwork = TonNetwork.MAINNET,
        refresh: Boolean = false,
    ): List<BannerEntity> = withContext(Dispatchers.IO) {
        val data = if (refresh) {
            loadRemote(network) ?: loadLocal()
        } else {
            loadLocal()
        } ?: return@withContext emptyList()
        filterHidden(walletId, data.banners)
    }

    fun hideBanner(walletId: String, bannerId: String) {
        hiddenDataSource.hide(walletId, bannerId)
    }

    private fun filterHidden(walletId: String, banners: List<BannerEntity>): List<BannerEntity> {
        val hidden = hiddenDataSource.getHidden(walletId)
        if (hidden.isEmpty()) {
            return banners
        }
        return banners.filter { it.id !in hidden }
    }

    private fun loadLocal(): BannerDataEntity? {
        return localDataSource.getCache(CACHE_KEY)
    }

    private suspend fun loadRemote(network: TonNetwork): BannerDataEntity? {
        val data = remoteDataSource.load(network) ?: return null
        localDataSource.setCache(CACHE_KEY, data)
        return data
    }

    private companion object {
        private const val CACHE_KEY = "banner_data"
    }
}
