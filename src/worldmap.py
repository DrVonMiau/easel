"""Offline vector world map for the Map view.

A compact set of world coastline/land polygons so the Map view can draw a
crisp, resolution-independent world with no map tiles, no network and no extra
dependency \u2014 Easel is offline-first. Derived from Natural Earth\'s public-domain
1:110m land polygons, simplified and quantised to 1/100\u00b0, then stored
zlib+base64 so the whole planet is a few kilobytes and decodes in a blink.

`land_rings()` returns the polygons as lists of (lon, lat) points in degrees;
the map widget projects and fills them. Rings include island and lake rings,
so fill with the even-odd rule to get holes right. Decode is lazy and cached.
"""
import base64
import struct
import zlib

# zlib(base64) of: uint16 ring count, then per ring uint16 point count followed
# by int16 lon*100, int16 lat*100 pairs (little-endian).
_PACKED = (
    "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"
    "WB2OdVYPx3Drdrup3dz3TzvJRmhf2/NvW2LPhTao50SrrntRy/BuVouxy9L8/Wd/40nrkcYZVlrjQuvKhiPWwIZsq1fDMevt"
    "hgjr+QZrR1nDx46ihrMdGxpEHZp6bfsdS2l7mgVt32GxtYktTW2vzZz2k+butqvm0216869tEstfbeGW+W1HzIS2ReblraQB"
    "vDaFtV40NbbMNh9tiTKHtOSY+pr3mg40O43KZpnpY9MC07GmWuOZpsnGaU2JRlvjPYO4KU1/tmmL3tF8Uz+nhW643/Kr/mPL"
    "D717a5c+r3Wx4VbrecPGtqmGo23hBnZbj17WFmlY2r7UcL893rC9Y5Vhd4fEOK2Tb0z/OdYY2nXeOKJ3n7G0V2SMtI3XD+8L"
    "17/uq9AGORK1Fx2LtB8dIVovZ7m225mt9XBd0nY652nlrkXas66D2tuu19qxQLDuCDBHB7md0LHc6nR73NJ1F9wydVcIm3Rt"
    "hN90ccRm7Q7iSa0vMUvbSVimLSZM07KIC+rGEqfWHSbm13YQs2sziUdqdcQDtVPBbA2dZNTwSD80waT1miGkOTUbSMTqKeQR"
    "VRj5dLkn9LXsK/S8rB8qKSNRdGVzKX+XmSj3Skupk0qd1PGlU2i+JePp338EMg78mMrw+7GaEfbjE31pyUpaXslOak/JH9TE"
    "UncqqbSR0lTiTVWU3Kd6lPjSWD9KaLE/ymjLf/jStcVH6YriBsaLotFMfeEB5vKCd+w/CpZzZhW85Fws+MQdXVDMqy/I5WGF"
    "S3nhhbt5YOEQfmv+M35kfjXfLT9UwM7fIDjx/bJgx/dmAel7mJDwfblw07fHwl++kURlX6eJrn99Ibrx5Y1ozucEce7H9eLD"
    "eZ/EcN5zsSJvrTgzFxU/zv1FXJ2bJk7+UC72+9AuLstRiuW5TlF2zmtRx/u1Iux9nfDGuzjht6wy4YhsnXDx+x2ie+9BcW12"
    "sehKNkm8N1snmp3NF2dkNYk+ZI0W789aJyZkzRaveHdrANVi9B1Z8t+7B5KxWTclIdkNkvosH2lw9njp6iwP6cl3gVLH22Tp"
    "1Ld3pdnvCDA/a+Cdk1UorcgqkS559xdc8/YFLHhXDydmSZHsrN+Rsvda5FXOVaTh4zLk8ZdGeNfXEIT/zRsZ+30Vcup7AmL9"
    "fgjR5g9BlhYsRzYVoMixwiSkv7gJ3vvjP7i7+CDMLD0Oby2dBJtKD8C7Sp/A70qL4MNlWjim/DQ8r2INfLRyG7y78j+4svIL"
    "/GvVPXhT1Uj4edUFGKw+CwdV98MTqxYgM6oKkIIqCfpfdQ0SV7MKsVSrEfwdJwOW/TiJhv1YgApKdqPXSsainNLd6KrSeeib"
    "UgfSVuqN3is9hi4uO4qWlC1Cg8szkOMVM5F9FQqkrtwLkVcAiKO8Gr5cRkbe/MiD//ejC75dTEL8iqcgopIdiLXoOPKm6Dly"
    "u7gamV30E+kqWoa6/biF4nuCgKL8MVpWthpVlz1DcU4BFPrjaIR+E9qpXYku195G12g/o1e0hehe3QcUr4UCqtq/0F21U9Hq"
    "2jtoRV0Bul+7HN2s9UIv6lahbgYqyjJokE+Gn4jK+AL53bQYKTdBiNxUAd8374HVFk+43CKD/7NUSCmWxdJ2U5t0rUkvXWj6"
    "R7rd7CV9Y86RXDL/IfEwWSTe5iBJgvGM5JMhV9Jk0Eo+6JOkeXpv6W2dWDpBFyU9rdsnRQ2npP36l9JrhhHwPsNm2KZrgKP0"
    "RMRHtwIZpo1BZmv2IFRNKrJBU4wYaoqRnJpIlKM5hW7RpKO/15ag+GdhAN8qKtDuykKUU/UcfV65Dx1VNR2NrHIioZVapLt8"
    "MtpbvgnNrViGTqu4h+JZEJBEXkBvhv+D7oyoQnFOAc5Xr0EDqz3QjCovdGfVU1RU3YXerqlCOTV7ULxGA6yFRqSp4BlSkM9H"
    "zxZ8QkMLSJiguB/VFb9A1xZNQa2FLBTPkACdegq2Vh2ICaLmYAFROzFck4B11aOwyOrBmKIqGeusOobhGgIeRe7H5kaqsRHh"
    "ntiesKXY67AHWHT4Wwz3qcDByl6suPILdq9yDEasTMDOV0zCLlYcxbrL92Nt5cUYXmcDGaW5mL70BpZa9gh7VbYd+71sLHb6"
    "Bxs78EOFTS2ZjZGKF2LNxVpsSekfWFlJNRZc6sIulP7E8D4+sE10HekVpCIaQRHiI2hCYMEgdLcgAt0uWIMmCnNQi6AALRN6"
    "YxnCKZhJ/Bm7DrNkJtiMsSRLsQoRhN0RJqPpQiuCz2EDYLVMFlHjxI7XaLAtNa8xX81hbLz2DFakTcIcdQLslIaNmauHYq+r"
    "U7HJ1YUYp6ofC6saIsP7IOBy8VTZ8WKKLL7YhB0pvIMRCzOxbUXDZbh/lvDZlysz+mqxxf492N8BN7AdAWuwTL8+1CvAjM4I"
    "uIN6BhagSxXv0J2Kk6i7ag/qpcpHLwa9QaODD6ErgkegimAZOjLkLvJPCAcdFWpAboW3ILPCR6F14ZvRpepQNDv6IYLGPEHW"
    "xRxAFsUMRWpj3JGT8QokJqEUTk/oglckhCF/JyxEcpNR/PYLT0hZKC1IDpOOTR4mvZtMkWIpGyTElGmSEcmrJRWJQsnhhMXi"
    "vvh/xN1xs8Sf4naLl8dNFetjF4lHxWaJxsaeF72L3St6FtsunBrbLPSLvSCcErtWeDtGLdwb80nQMuypoGDYKEHssBb+66E3"
    "hZahgaKhw5aL4oedE9UMeyxqi9WLY+PWSfJil0mOxAokR2PuiNUxgySRw1zimKHTxSeHImJxdLdIFh0j3qseJyarUfHRCI74"
    "c+gC4dKwKcJzYSJhbxggjAwPEt4J9xdGRLwRqCOWC5rDQcGr8BaeM0zMexL6k+MXmsZ5GFLP+RC8kPMguJddFHSInRM0kT04"
    "WMg+HTyXdSa4ibkm+AVzRdA65vqgdmZS0AzW8aAVrJFB71jtqtes1yoDq1GFsH+qNrH/VF1gj1QGs9OUX9j7lS72TsVUNqqY"
    "wG4I9GDLFLWs9Yp81ibFDdZ7xX5WsPIr65vyBqtGuYY1TjmR9afiFrNHsZp5TbmAcUUpZsxV2un/Ku/SDykh+kSlkzZTcY6a"
    "HGimnAt4TNkcUEAZGnCFMtx/NmWIvxVq8WuGGv1mUxb7bac89RVRZvu+hlJ8N0KH/HpJr/xiSff8ToAC39nEdp/LRIavlrjW"
    "xxvk++SAi7zbwI3ew0jwAN57XSYSvS8QY7wthEgfH8JCH7PbWh+x22ufPcB6nz+AS94CtyHefILU+yBhm1cKcaUXnfiLVyLx"
    "L6+t4E/PK+R4z8Hkr0OSyC1DssgVQzDoyJBT0NkhLyHhED7l1WATZebgBkri4EOUjx5XKWkev1P6Bi2luAbBlC+D6iD1oMsQ"
    "4n6HjLhvJc+Xh5OD5QzQKYPAIlkGsU12kfiXbAdxlAwjNmHbiDFYHyjAXpHS0FjyPHQ6JR9BKf9DfCggcoMSinygiJFmynR4"
    "JrVF2kYlS75RV4tYtHjRYRpXdJE2Xzid5i4w0P7if6TL+BX0N7xXdC6/iHaOv4Z2hu9Bq+Q/oLbwfWneAgGNL7hGjRYspEqF"
    "q6mgaAKNIsqkhom2UjtEYdQ54teUSlE/dE5UDx0WLYUwUTxkF9rJ54SGAWSQtwiWkhcIJOQ5/HckkL+V9IMbRDrD7QbtnHYw"
    "ldNB+odzjOxkX4FGsSdSEtnFFCo7nprK2kFNY3bT/jeAVOZBehzzO+0+4x/6JGY4rYM5g6pjkqnLWOcpqaxkyI1NgX5hR5Kb"
    "OeVgA+cR+JRzFhzKFYJruAKwh/8M7OD7gBP59wh3eHfc0tku5wR2gNOHvdWxnD3FMZ79xF7I/trfyk7sW8HearvCGmQ7x0zp"
    "Pc509hiZ83oamG+6nzLXd6cxOrtqGO+7/mJ8+Emmh3TyaG+tRZQ8631oqVUB3be6QYmdg6CpnTLoZmcHaVXXadKcnktgR+9F"
    "sNSmB1/2TSaN6Z9BKuinkFbZA0j37PdAinM98ZSrhBji9pC42W0SUeM2jnDf7V+3TYQMYClhqKvcTekkETiOZ27b7QY3z/4y"
    "QmhfHLjPlg3+bptAktmySadt48j/2vhQqq0SmmYLpcyzHaO8tyVRkb6/qLP74mm8/p+0df0uWpL9X7q/XcGYa/+PMdYxmfnE"
    "0cv0dr1hngHGsgSE6ayNhF2sUcR1rNfEcywH8U9WH3E6axq4kjUEjGKlgFZmFHiJ2UNcwaSBIcwdoJXRRbrEGExOYGyGftBL"
    "IYTxFlrG0EDnGSspJQwrJYshoJ5hlFCnMmbSfmGMprMZMGMKYwFjOGM7Q8g4wwhnBDGljN3MyQwO6xBTznIy+5gBLHfWRNYJ"
    "5nzWaKYX6zkDYS1kTGB106ewLtJlrKiB9RttEmsErYW1lRbNjqKtZN+jfWEn0PPZq+kwx5/B5IiZKzlvmKs5/SwX+wObxInm"
    "qDmjOIs5QZx/OFNYD7mXWZncNhaH95oVyRvB/o13j8nnPWaWcpNZOdwg1jXuUOYULom5nIswz3EfMV5y5cwGLoEJ8JYypLz5"
    "jFgekRHMO0M/z02i/8n1ofO5BbSbHDstgTOZ7sGJp1M5X2ggZwztCVtBs7LfUF3sRKqJfZj6iD2NepndQClkl1PS2XLqeLaN"
    "EsVeRG1lLaa+YlGohSxP6hWWmXKGRaeqWZ8pUawtlCWsOEoP628ojX0TGsNJILs4LaQR3GLSZe5p0gPuJtJu7jTSTe5GUgHX"
    "g1TLLQLTuU1gFLeRtJgTRV7MmUgexwkjqzmR0L9sT+gY+xz5G3sN+Sj7JXkp+x9yMHsUuZzlTi5gjSZHsknk2+wMcBQHAT9w"
    "7EQf7u/EqdxjhErOJ7efnHC3Eo7KbQ6nCEA4EwESx+iaxaYDHuxM1wvWetd1lq9rMmu0cwrL7KCzXjr4rF8da1kH7ctYR+2d"
    "rOF2D/YZezN7p/0WZ6q9gKO3i7kJrm7Ob65g7mGXjVvh/JsX68zkhTlB/lwXwI91beRfcQ3nHwT+5qcCFv4uNz9BudsbQR7B"
    "JgggNgjWE2FhAPGp0JcoEk0jjhd5gMdEbmCUKBFkiT4QHwj1xCnC4eBQYQooFRrB4cL9JECoJ68RPoYShR+gdcLxlBPCYRSF"
    "aC1ljegb5YQIpSaLVlOHi/6k5ou8qSaRjCoXw7SR4lbaUPF8+iZxD+2muI2yQDyF8p94JQWRqCmbJSspTyTN1HVSHfWCdBr1"
    "s/Q+5aF0JGWZdAbFV2oj75M8JXeLf4G2ivvJkPgMuV0USm4UZpB+CveSbgtZpOtCBhgtTiF+FGUTnooiCAZRp1uYBAODpLmk"
    "HfBOKAVxUlYj56jvEDd6ARLFOICk0VchXowA5BhjPPKGfRimcWbCXI4Yvsk2SJ2sT9JuBgRfYL6V3mH6SjezOiR7WYOkSaxx"
    "0oOsjdIt7FDpN/YY6SH2WekGTq/Un1sgXcIVwDzueDiR+yds43yGubx/4FG8DfA37pIBBMIIjw6/5LnD+bz5sF1ghocLdbBJ"
    "kAovEF6Ee0X18ErxQ7hG3ApvEvsgd8UjkXyJP5IDX4DJSD58FZYgnvAgJAWeiDDhd0geLEUpyK/oLHQtuh4NR7moCVmH7kKm"
    "o93wRzQNrkDqpaPQSulEbCssxv6FU7BOuALth7PQEISDnUOOow1IPBaA+mGb0NnYb+hJLAZdgdUiJdhX5A7GR8fJolFMHoeW"
    "ySF0h3wmukn+AW0cpEcvDBJhTI/p2KohL7Fcz3vYbO9qrNw7SJbjM1O2zjdKZvEZJMPvlDJAyXfnPOGns+fwn7FFfD92JP8x"
    "q4a/jAUJN7AYwgj2W8EMdoXgKvtvwS32Q0EvmyasYy8Uuth2wa+ch4JZnBsCHmev4DWHJajk3OWv4VIEi7gMwSPuf4LHAxjL"
    "c/EX8BR8d95Q3hruOR6Le4z3lYPvSQa6SmbKokuGye7+mCM7UvI/mbh0qwz3IOAedYXsMyVOJqVMl82lbJQNopyStVNuy3Af"
    "BPxKX8hoJTdkG0u+y3BOAk5V7ZQ9rNwt+72yQPaw6qYM1xSgseK8jFu5TtZYdlU2ujxTVlxWIvta1ie7UvFJhtfIAMP3nKzC"
    "a73suHenTOftkvn6vpPhHhcYBHnIV1IqZOugp7IIaJmsjjxF9pTsJosgO7D5JLUsgzRXthAskAlATO5JipfPJsnkjSS5nENO"
    "kN8iJ8vxfgrQQV0hf0lLkbvTOHInBZRXQEL554F7zE/KXjleowF9/C3yc7x4uR9vhnwRb6v8APcPuZZ3XP6Tt0l+lH9Z7i44"
    "KMczFIDmXSiTemplm4ZAcu8h4+RXBy+Ub/N8Ix/s7SfHa1yAUCuT/1HrlEVq/pYdqjku+6/6omxsdY5sXLVFdqWaILdUucmj"
    "qibIc6suyf2qb8qdVWlyS/Vb+bKaQ/LnmvlyvH8wEGPa4u5uWehebhnu/sMS4H7aVCZfbS6Xa4w75cHGAHmXzi7bpW+XqfW1"
    "smP6R7IKra/sjM6JHdK+x+iaOsxZw5a91njJDmrWyvJrR8lSao/IUjVXZDm1LllYrZecXjtDvqpulBzSrpV/0vwqF2vOyqNq"
    "HPKLtSPd99b5uOfXzXUn6X53x8/xC7Cy67p7dc9E98E9fu6ZnTT3Md2dclYP131BT4u8s/u5vNzmkhP7m+XR/dnyVbZFcv/e"
    "SXK+bax8XG+ObGLvFNnnXr7sUE8bFt+bjk3vXYK19MzG1vWSsVM9TpTUW4Yae56hX7tvoSN75qGjeoLR/d2z0Ynd8eisniZk"
    "T89FJL/7fwi524L4dN9CXnStQ5J7liC+Xe/gs52X4VQrA97UflqKtU+RNrR9k6S0bZesbVNIWG3bxHtafxW3tnwQe7XkiUub"
    "qiV/Nf0mpTUVSxVNKNzUGAuPa3oBG5sq4UHNExFZEwdJbQxCchvvIy1Np5D6xk4kqNGBXG+chV5uUGJowzQMa9iNcevTsRjT"
    "Kww1CmXNJrVsrmG7bLXhlmyqmSjfbvaRrzWNkf9ryZX/z9Ihv9uAuI9ocsgnNoe6X241yx+3oO6ZLcPd6W373EdbH7njz/X/"
    "AE6WA04="
)

_rings = None


def land_rings():
    """The world\'s land as a list of rings; each ring is a list of (lon, lat)
    floats in degrees. Cached after the first call."""
    global _rings
    if _rings is not None:
        return _rings
    data = zlib.decompress(base64.b64decode("".join(_PACKED)))
    off = 0
    (nrings,) = struct.unpack_from("<H", data, off)
    off += 2
    rings = []
    for _ in range(nrings):
        (npts,) = struct.unpack_from("<H", data, off)
        off += 2
        pts = []
        for _ in range(npts):
            lon, lat = struct.unpack_from("<hh", data, off)
            off += 4
            pts.append((lon / 100.0, lat / 100.0))
        rings.append(pts)
    _rings = rings
    return _rings
