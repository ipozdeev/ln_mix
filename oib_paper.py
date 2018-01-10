import pandas as pd
from pandas.tseries.offsets import DateOffset
import numpy as np
from os import listdir
import pickle
import matplotlib.dates as mdates

from foolbox.data_mgmt import set_credentials as set_cred
from foolbox.linear_models import PureOls
from foolbox.utils import add_fake_signal

from optools import functions as op
from optools import wrappers as wrap
from optools.import_data import *

from optools.factormodels import FactorModelEnvironment

from optools.oib_paper_settings import *

class OIBPaper():
    """
    """
    def __init__(self, tau_str="1m", ccur="usd", x_curs=[]):
        """
        """
        self.tau_str = tau_str
        self.tau = float(tau_str[:-1])/12.0
        self.ccur = ccur
        self.x_curs = x_curs

        self.path_main = set_cred.gdrive_path("option_implied_betas_project/")
        self.path_to_fx = set_cred.gdrive_path("research_data/fx_and_events/")
        self.path_to_raw = self.path_main + "data/raw/"
        self.path_to_mfiv = self.path_main + "data/estimates/"

        # construct hangar name
        self.hangar_name = self.path_to_mfiv + '_'.join(
            ("data_vs", ccur, tau_str, "x"+ str(len(x_curs)) + "curs.h5"))

    @property
    def mfiv(self):
        if "mfiv_" + self.tau_str + ".h5" in listdir(self.path_to_mfiv):
            with pd.HDFStore(self.path_to_mfiv + "mfiv_" + self.tau_str +
                ".h5", mode='r') as hngr:
                res = hngr["mfiv"]
        else:
            res = self.calculate_mfiv()

        return res

    @property
    def mficov(self):
        try:
            res = self.from_hdf("mficov")
        except (OSError, KeyError) as e:
            res = self.calculate_mficov()
        return res

    def calculate_mfiv(self):
        """
        """
        path_to_options = self.path_to_raw + "options/"

        # interest rates
        with pd.HDFStore(self.path_to_raw + "ir/ir.h5", mode="r") as h:
            ir = h["/ir_" + self.tau_str]

        # fetch all files with raw data
        files = list(filter(
            lambda x: x.endswith("deriv.xlsx"), listdir(path_to_options)))

        mfiv = pd.DataFrame()

        for filename in files:
            # collect data from .xlsx file
            # filename = files[17]
            # filename = 'usdjpy_fx_deriv.xlsx'
            print(filename)

            data_for_est = import_data(
                data_path=path_to_options,
                filename=filename,
                tau_str=self.tau_str,
                ir_name=ir)

            # e.g. for Denmark
            if data_for_est.empty:
                continue

            this_mfiv = pd.Series(index=data_for_est.index)

            for idx, row in data_for_est.iterrows():
                # idx, row = list(data_for_est.iterrows())[-528]
                # fetch wings
                deltas, ivs = op.get_wings(
                    row["rr25d"],
                    row["rr10d"],
                    row["bf25d"],
                    row["bf10d"],
                    row["atm"],
                    row["y"],
                    self.tau)

                # to strikes
                K = op.strike_from_delta(
                    deltas,
                    row["s"],
                    row["rf"],
                    row["y"],
                    self.tau,
                    ivs,
                    True)

                # concat to pandas object
                vol_surf = pd.DataFrame(
                    data=np.vstack((K, ivs)).T,
                    columns=["K", "iv"])

                # transform & integrate
                res = op.mfiv_wrapper(
                    vol_surf, row["f"], row["rf"], self.tau, "spline")
                this_mfiv[idx] = res

            mfiv[filename[:6]] = this_mfiv

        with pd.HDFStore(self.path_to_mfiv + "mfiv_" + self.tau_str +".h5",
            mode='w') as hngr:
            hngr.put("mfiv", mfiv)

        return mfiv

    def from_hdf(self, what, s_dt=None, e_dt=None):
        """
        """
        with pd.HDFStore(self.hangar_name, mode='r') as hngr:
            res = hngr[what]

        if (s_dt is None) and (e_dt is None):
            return res

        if s_dt is None:
            s_dt = res.index[0]
        if e_dt is None:
            e_dt = res.index[-1]

        return res.loc[s_dt:e_dt]

    def to_hdf(self, what_dict):
        """
        what_dict : dict
            {key : value}
        """
        with pd.HDFStore(self.hangar_name, mode='a') as hngr:
            for k, v in what_dict.items():
                hngr.put(k, v)

    def calculate_mficov(self):
        """
        """
        # x_curs = ["dkk","nok","sek"]
        # collect all variances into one dataframe --------------------------
        variances = self.mfiv

        # exclude some ------------------------------------------------------
        filter_fun = lambda x: all([p not in x for p in self.x_curs])
        all_pairs = list(filter(filter_fun, variances.columns))
        variances = variances[all_pairs]

        # estimate covariances ----------------------------------------------
        unq_curs = []
        for p in variances.columns:
            unq_curs += [p[:3], p[3:]]
        unq_curs = [c for c in list(set(unq_curs)) if c != "usd"]

        covmat_panel = pd.Panel(
            items=variances.index,
            major_axis=unq_curs,
            minor_axis=unq_curs)

        for idx, row in variances.iterrows():
            # row = variances.loc["2012-04-13"]
            covmat, _ = wrap.wrapper_implied_co_mat(row, self.ccur)

            # save matrices to Panel
            covmat_panel.loc[idx] = covmat

        # drop na
        covmat_panel = covmat_panel.dropna(axis="items", how="all")

        # store covmats and correlations
        self.to_hdf({"mficov": covmat_panel})

        with pd.HDFStore(self.hangar_name, mode='a') as hngr:
            hngr.root.mficov._v_attrs.x_curs = self.x_curs

        return covmat_panel

    def calculate_mfibetas(self, wght=None, trim_vcv=False, exclude_self=False,
        mnemonic=None):
        """
        """
        # fetch covariances
        vcv = self.mficov

        # record names, for later use (this will be an Index)
        curs = vcv.minor_axis

        if wght is None:
            if mnemonic is not None:
                raise ValueError("For undefined weights, equally-weighted " +
                    "betas are calculated and stored under 'eq'.")
            else:
                mnemonic = "eq"
            wght = pd.DataFrame(1.0, index=vcv.items, columns=curs)
        else:
            if mnemonic is None:
                raise ValueError("Provide mnemonic for identification of " +
                    "the calculated series in the HDF.")
            if isinstance(wght, pd.Series):
                wght = pd.DataFrame(
                    data=np.array([wght.values, ]*len(vcv.items)),
                    index=vcv.items,
                    columns=curs)

        # estimate betas ----------------------------------------------------
        B = pd.DataFrame(index=vcv.items, columns=curs)
        vix = pd.DataFrame(index=vcv.items, columns=curs)

        # loop over dates
        for idx, row in vcv.iteritems():
            # idx = vcv.items[10]

            # skip this matrix if any NA is present
            this_vcv = row.copy()
            this_wght = wght.loc[idx].copy()

            if this_vcv.isnull().any().any():
                continue

            # trim if necessary (+reweight!)
            if trim_vcv:
                this_vcv = self.trim_covmat(this_vcv)
                this_wght = wrap.normalize_weights(this_wght)

            this_b, this_vix = wrap.wrapper_beta_from_covmat(
                covmat=this_vcv,
                wght=this_wght,
                exclude_self=exclude_self)

            # save
            B.loc[idx, :] = this_b
            vix.loc[idx, :] = this_vix

        # rename
        B = B.loc[:, sorted(B.columns)]
        vix = vix.loc[:, B.columns]

        # store
        self.to_hdf({
            ("mfibetas/" + mnemonic): B,
            ("vix/" + mnemonic): vix,
            ("wght/" + mnemonic): wght})

        return B

    def calculate_olsbetas(self, assets, wght=None, align=True,
        method="simple", exclude_self=False, mnemonic="none", **kwargs):
        """
        """
        vcv = self.mficov

        if wght is None:
            wght = pd.DataFrame(1.0,
                index=assets.index,
                columns=assets.columns)

        if isinstance(wght, pd.Series):
            wght = pd.DataFrame(
                data=np.array([wght.values, ]*assets.shape[0]),
                index=assets.index,
                columns=assets.columns)

        if align:
            assets = assets.loc[:, vcv.minor_axis]
            wght = wght.loc[:, vcv.minor_axis]

        # construct factor environment
        factor_env = FactorModelEnvironment.from_weights(assets, wght,
            exclude_self=exclude_self)

        if exclude_self:
            B = {k: v.get_betas(method=method, **kwargs).loc[:, "factor"]/
                .rename(k) for k, v in factor_env.items()}
            B = pd.DataFrame.from_dict(B)
            F = {k: v.factors for k, v in factor_env.items()}
            F = pd.DataFrame(F)
        else:
            # calculate betas
            B = factor_env.get_betas(method=method, **kwargs)
            F = factor_env.factors.squeeze()

        # store
        self.to_hdf({
            ("olsbetas/" + mnemonic): B,
            ("dol_idx/" + mnemonic): F,
            ("wght/" + mnemonic): wght})

        return B

    def calculate_olscov(self, assets, wght=None, align=True,
        method="simple", exclude_self=False, store=False, **kwargs):
        """
        """
        vcv = self.mficov

        if wght is None:
            wght = pd.DataFrame(1.0,
                index=assets.index,
                columns=assets.columns)

        if isinstance(wght, pd.Series):
            wght = pd.DataFrame(
                data=np.array([wght.values, ]*assets.shape[0]),
                index=assets.index,
                columns=assets.columns)

        if align:
            assets = assets.loc[:, vcv.minor_axis]
            wght = wght.loc[:, vcv.minor_axis]

        # construct factor environment
        factor_env = FactorModelEnvironment.from_weights(assets, wght,
            exclude_self=exclude_self)

        if exclude_self:
            B = dict()
            for k, v in factor_env.items():
                this_b = v.get_betas(method=method, denom=True, **kwargs)
                b = this_b.loc[:, "factor"].mul(
                    this_b.loc[:, "denominator"], axis=0)
                B[k] = b

            B = pd.DataFrame.from_dict(B)

        else:
            # calculate betas
            B = factor_env.get_betas(method=method, denom=True, **kwargs)

        # store
        if store:
            self.to_hdf({
                ("olsbetas/" + method): B,
                ("dol_idx/" + method): F,
                ("wght/" + method): wght})

        return B

    @staticmethod
    def trim_covmat(covmat):
        """
        """
        covmat_trm = covmat.copy()

        # init count of nans
        nan_count_total = pd.isnull(covmat_trm).sum().sum()

        # while there are nans in covmat, remove columns with max no. of nans
        while nan_count_total > 0:
            # detect rows where number of nans is less than maximum
            nan_max = pd.isnull(covmat_trm).sum()
            nan_max_idx = max([(p,q) for q,p in enumerate(nan_max)])[1]

            covmat_trm = covmat_trm.drop(covmat_trm.columns[nan_max_idx],
                axis=0)
            covmat_trm = covmat_trm.drop(covmat_trm.columns[nan_max_idx],
                axis=1)

            # new count of nans
            nan_count_total = pd.isnull(covmat_trm).sum().sum()

        return covmat_trm

    @staticmethod
    def smooth_to_monthly(x, wght):
        """
        """
        my_filter = lambda x: x.ewm(alpha=wght).mean()
        res = x.resample("M").apply(my_filter).groupby(level=0).last()
        # for pandas v 0.20.+
        # res = x.resample("M").apply(my_filter).groupby(
        #     pd.Grouper(freq='M'), level=-1).last()

        return res

    def table_predict_betas(self, b_real, b_impl=None, both=False):
        """
        b_real = B_real.loc[:, B_impl.columns]
        b_impl = oibp.smooth_to_monthly(B_impl, 0.9)
        """
        res_coef = dict()
        res_tstat = dict()
        res_r2 = pd.Series(index=b_real.columns)

        for c in b_real.columns:
            # define y
            y = b_real.loc[:, c].rename("y")

            # define X conditional on input
            if (b_impl is not None) & both:
                X = pd.concat((
                    b_real.loc[:, c].shift(1).rename("self"),
                    b_impl.loc[:, c].shift(1).rename("impl")), axis=1)
            elif (b_impl is None) and (not both):
                X = b_real.loc[:, c].shift(1).rename("self")
            elif (b_impl is not None) and (not both):
                X = b_impl.loc[:, c].shift(1).rename("impl")
            else:
                raise ValueError("Impossibru!")

            # model + estimate
            this_mod = PureOls(y0=y, X0=X, add_constant=True)
            this_diag = this_mod.get_diagnostics(HAC=True)

            # store output
            res_coef[c] = this_diag.loc["coef", :]
            res_tstat[c] = this_diag.loc["tstat", :]
            res_r2.loc[c] = this_diag.ix["adj r2", 0]

        res_coef = pd.DataFrame.from_dict(res_coef)
        res_tstat = pd.DataFrame.from_dict(res_tstat)

        return (res_coef, res_tstat, res_r2)

    def fig_carry_vs_flb(self, rx, fdisc, which="mfibetas/eq", n_portf=3,
        add_fake_sig=False):
        """Create figure with carry and flb."""
        # fetch implied betas -----------------------------------------------
        b_impl = self.from_hdf(which)

        # smooth to arrive at monthly betas
        b_impl_m = self.smooth_to_monthly(b_impl, wght=0.9).ffill()

        # fake signal? ------------------------------------------------------
        if add_fake_sig:
            rx, b_impl_m = add_fake_signal(rx, b_impl_m)
            _, fdisc = add_fake_signal(fdisc, fdisc)

        # get flb (sort by b_impl_m) ----------------------------------------
        flb = poco.get_hml(rx, b_impl_m.shift(1),
            n_portf=n_portf).rename("flb")

        # get carry ---------------------------------------------------------
        carry_smpl_name = "carry_"+str(10-len(self.x_curs))
        carry_smpl = poco.get_hml(rx.loc[:, b_impl_m.columns], fdisc.shift(1),
            n_portf=n_portf).rename(
            carry_smpl_name)

        carry_all = poco.get_hml(rx, fdisc.shift(1),
            n_portf=n_portf).rename("carry_10")

        # concatenate and dropna --------------------------------------------
        both = pd.concat((flb, carry_smpl, carry_all),
            axis=1).dropna(how="any")

        # add zero
        both.loc[both.index[0]-DateOffset(months=1), :] = 0.0
        both = both.loc[sorted(both.index)]

        # plot --------------------------------------------------------------
        fig, ax = plt.subplots()

        # ticks
        ax.tick_params(axis='x', which='minor', bottom='off', top="off")
        ax.xaxis.set_major_locator(mdates.YearLocator(1))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))

        both.cumsum().plot(ax=ax, color=[my_black, my_red, my_blue],
            linewidth=1.5)

        # ax.set_xlim((
        #     both.index[0]-DateOffset(months=3),
        #     both.index[0]+DateOffset(months=3)))

        ax.annotate(
            r"$/rho={:3.2f}$".format(both.corr().loc["flb", carry_smpl_name]),
            xy=(0.95, 0.25),
            xycoords='axes fraction',
            backgroundcolor='w',
            horizontalalignment='right',
            fontsize=14, color=my_red)
        ax.annotate(
            r"$/rho={:3.2f}$".format(both.corr().loc["flb", "carry_10"]),
            xy=(0.95, 0.15),
            xycoords='axes fraction',
            backgroundcolor='w',
            horizontalalignment='right',
            fontsize=14, color=my_blue)

        ylim_min, ylim_max = ax.get_ylim()
        ax.set_ylim((ylim_min, ylim_max+0.1))

        ax.legend(loc="upper left", prop={"size":12})

        # labels
        ax.set_ylabel("return, in frac. of 1")
        ax.set_xlabel('', visible=False)

        plt.setp(ax.xaxis.get_majorticklabels(), rotation="horizontal",
            ha="center")

        fig.tight_layout()

        fig.savefig(self.path_main + "tex_nnew/figs/flb_vs_carry_" +/
            str(n_portf) + "portf_x" +/
            str(len(self.x_curs)) + "_cur.pdf", transparent=True)

        return fig, ax


if __name__ == "__main__":

    from foolbox.api import *

    # parameters ------------------------------------------------------------
    tau_str = "1m"
    ccur = "usd"
    x_curs = ["sek", "nok", "dkk"]

    # data ------------------------------------------------------------------
    with pd.HDFStore(data_path + "data_wmr_dev_m.h5", mode="r") as hangar:
        data_m = dict(hangar)

    rx_m = data_m["/rx"]
    fdisc_m = data_m["/fwd_disc"]

    with pd.HDFStore(data_path + "data_wmr_dev_d.h5", mode="r") as hangar:
        data_d = dict(hangar)

    fdisc_d = data_d["/fwd_disc"]
    s_d = data_d["/spot_ret"]
    s_m = s_d.resample('M').sum()

    # instance --------------------------------------------------------------
    oibp = OIBPaper(tau_str=tau_str, ccur=ccur, x_curs=x_curs)

    # calculations ----------------------------------------------------------
    # mfiv = oibp.mfiv

    # covariances
    # mficov = oibp.calculate_mficov()

    # mfi betas
    B_impl = oibp.calculate_mfibetas(trim_vcv=False, exclude_self=True)
    B_impl = oibp.from_hdf("mfibetas/eq")
    B_impl = B_impl * oibp.from_hdf("vix/eq") * 10000
    B_impl_m = oibp.smooth_to_monthly(B_impl, 0.9).fillna(method="ffill")

    B_impl_m.dropna().describe().round(2)
    (fdisc_m.loc[B_impl_m.index, B_impl_m.columns]*10000).describe()

    # ols betas
    B_real = oibp.calculate_olsbetas(assets=s_d,
        align=True, wght=None, exclude_self=True,
        method="simple")
        # , by=pd.Grouper(freq='M'))

    B_real_m = oibp.from_hdf("olsbetas/grouped_by")

    # PCA -------------------------------------------------------------------
    from foolbox.linear_models import PrincipalComponents
    pca = PrincipalComponents(s_d.loc[:"2008-06", :], n_comps=2)
    pca.estimate()
    pca.plot(cumsum=True)
    pca.rotate_components()
    pca.plot(cumsum=True)
    pca.loadings
    new_wght = pca.loadings.loc[:, "pc_1"]
    new_wght = wrap.normalize_weights(new_wght)
    pd.concat((pca.fit(), s_d.loc[:, B_impl_m.columns].mean(axis=1)), axis=1)/
        .corr()
    pca.fit().corr()

    B_impl_new = oibp.calculate_mfibetas(trim_vcv=False, exclude_self=True,
        wght=new_wght, mnemonic="pca_based")
    B_impl_new_m = oibp.smooth_to_monthly(B_impl_new, 0.9)

    my_filter = lambda x: x.ewm(alpha=0.9).mean()
    res = B_impl.resample("M").apply(my_filter).groupby(level=0).last()

    # bis weights -----------------------------------------------------------
    bis_wght = {
        "eur": 1551,
        "jpy": 754,
        "gbp": 512,
        "aud": 301,
        "cad": 210,
        "chf": 250,
        "sek": 87,
        "nok": 52,
        "nzd": 63}

    bis_wght = pd.Series(bis_wght, name="bis").drop(x_curs, errors="ignore")
    bis_wght = bis_wght / bis_wght.sum()

    B_impl_bis = oibp.calculate_mfibetas(trim_vcv=False, exclude_self=True,
        wght=bis_wght, mnemonic="bis")

    B_impl_bis_m = oibp.smooth_to_monthly(B_impl_bis, wght=0.9)

    res = oibp.fig_carry_vs_flb(rx_m, fdisc_m, which="mfibetas/eq", n_portf=3,
        add_fake_sig=True)

    B_real = oibp.calculate_olsbetas(assets=s_d.loc[:, bis_wght.index],
        align=True, wght=None, exclude_self=True,
        method="grouped_by", by=pd.TimeGrouper('M'))

    B_real_m = oibp.from_hdf("olsbetas/grouped_by")
    B_real_m = oibp.smooth_to_monthly(B_real, wght=0.9)
    B_real_m.loc[:"2008-08"].describe()

    res = oibp.fig_carry_vs_flb(rx_m.loc["2008-08":], fdisc_m,
        which="olsbetas/grouped_by",
        n_portf=3, add_fake_sig=True)

    B_real_m.corrwith(fdisc_m)

    carry = poco.get_hml(returns=s_d,
        signals=fdisc_d.rolling(22).mean().shift(1),
        n_portf=5)


    path_lol = "d:/Personal/hedge_funds_stock_pickers/data/"
    with pd.HDFStore(path_lol + "pivoted.h5", mode="r") as hangar:
        # data_eqt = hangar["/ret"]
        print(hangar.keys())

    data_eqt_m = data_eqt.resample('M').sum()

    fme = FactorModelEnvironment(assets=data_eqt, factors=carry)
    betas = fme.get_betas(method="grouped_by", by=pd.TimeGrouper('M'))

    with pd.HDFStore(path_lol + "betas.h5", mode="w") as hangar:
        hangar.put("betas", betas)


    lol = poco.rank_sort(data_eqt_m, B.shift(1), n_portfolios=10)
    wut = poco.get_factor_portfolios(lol, hml=True)
    wut.describe()