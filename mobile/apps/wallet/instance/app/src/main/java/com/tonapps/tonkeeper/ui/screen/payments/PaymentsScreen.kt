package com.tonapps.tonkeeper.ui.screen.payments

import android.os.Bundle
import android.view.Gravity
import android.view.View
import androidx.core.view.doOnLayout
import androidx.core.view.updatePadding
import androidx.core.widget.NestedScrollView
import androidx.recyclerview.widget.RecyclerView
import com.tonapps.tonkeeper.extensions.isLightTheme
import com.tonapps.tonkeeper.ui.base.BaseWalletVM
import com.tonapps.tonkeeper.ui.screen.main.MainScreen
import com.tonapps.tonkeeper.ui.screen.send.contacts.main.SendContactsScreen
import com.tonapps.tonkeeperx.R
import com.tonapps.uikit.color.backgroundPageColor
import com.tonapps.uikit.color.backgroundTransparentColor
import com.tonapps.uikit.color.textPrimaryColor
import com.tonapps.uikit.icon.UIKitIcon
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
    private lateinit var contentView: NestedScrollView

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        headerView = view.findViewById(R.id.header)
        contentView = view.findViewById(R.id.content)

        // Шапка лежит поверх контента, и её высота = barHeight + вырез статус-бара,
        // поэтому отступ берём фактический, а не из константы
        headerView.doOnLayout { contentView.updatePadding(top = it.measuredHeight) }
        headerView.title = getString(Localization.payments)
        headerView.setTitleGravity(Gravity.START)
        // Тот же размер, что у крупного заголовка «История» (MoonTopAppBarLarge — h1, 32sp).
        // Стиль задаёт только размер и шрифт, поэтому цвет возвращаем из темы —
        // иначе он унаследуется от системного родителя и станет тёмным
        headerView.titleView.setTextAppearance(uikit.R.style.TextAppearance_H1)
        headerView.titleView.setTextColor(requireContext().textPrimaryColor)
        headerView.hideCloseIcon()
        headerView.setAction(UIKitIcon.ic_address_book_28)
        headerView.doOnActionClick = {
            navigation?.add(
                SendContactsScreen.newInstance(wallet, requestKey = CONTACTS_REQUEST_KEY)
            )
        }
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

        // Экран контактов возвращает выбранный адрес по этому ключу. Здесь адресная
        // книга открывается только для просмотра, поэтому результат никто не слушает
        private const val CONTACTS_REQUEST_KEY = "payments_contacts"

        fun newInstance(wallet: WalletEntity) = PaymentsScreen(wallet)
    }
}
