package com.tonapps.tonkeeper.ui.screen.payments

import android.os.Bundle
import android.view.Gravity
import android.view.View
import androidx.recyclerview.widget.RecyclerView
import com.tonapps.tonkeeper.extensions.isLightTheme
import com.tonapps.tonkeeper.ui.base.BaseWalletVM
import com.tonapps.tonkeeper.ui.screen.main.MainScreen
import com.tonapps.tonkeeperx.R
import com.tonapps.uikit.color.backgroundPageColor
import com.tonapps.uikit.color.backgroundTransparentColor
import com.tonapps.blockchain.model.legacy.WalletEntity
import com.tonapps.wallet.localization.Localization
import uikit.drawable.BarDrawable
import uikit.widget.HeaderView

/**
 * Каркас вкладки «Платежи»: пока только заголовок и заглушка, содержимое появится позже.
 */
class PaymentsScreen(wallet: WalletEntity) : MainScreen.Child(R.layout.fragment_payments, wallet) {

    override val fragmentName: String = "PaymentsScreen"

    override val viewModel: BaseWalletVM? = null

    private lateinit var headerView: HeaderView

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        headerView = view.findViewById(R.id.header)
        headerView.title = getString(Localization.payments)
        headerView.setTitleGravity(Gravity.START)
        headerView.hideCloseIcon()
        if (requireContext().isLightTheme) {
            headerView.setColor(requireContext().backgroundPageColor)
        } else {
            headerView.setColor(requireContext().backgroundTransparentColor)
        }
    }

    // Списка на экране нет — прокручивать и затемнять шапку нечего
    override fun getRecyclerView(): RecyclerView? = null

    override fun getTopBarDrawable(): BarDrawable? = headerView.background as? BarDrawable

    companion object {

        fun newInstance(wallet: WalletEntity) = PaymentsScreen(wallet)
    }
}
