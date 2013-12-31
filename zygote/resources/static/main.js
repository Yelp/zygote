var format10 = function (d) {
	if (d < 10)
		return "0" + d;
	else
		return d;
}

var mungeTimes = function (obj) {
	if (!obj) {
		return obj;
	} else if (typeof obj === 'object') {
		if (obj.hasOwnProperty('length')) { // overly simplistic array check
			var a = new Array(obj.length);
			for (var i = 0; i < obj.length; i++) {
				a[i] = mungeTimes(obj[i]);
			}
			return a;
		} else {
			var o = {};
			for (var prop in obj) {
				if (obj.hasOwnProperty(prop)) {
					if (prop.match(/time_/)) {
						if (typeof obj[prop] === 'number') {
							var d = new Date(obj[prop] * 1000);
							var s = d.getFullYear() + "-" + format10(d.getMonth() + 1) + "-" + format10(d.getDate()) + " " + format10(d.getHours()) + ":" + format10(d.getMinutes()) + ":" + format10(d.getSeconds());a
							o[prop] = obj[prop]
							o[prop + "_fmt"] = s;
						} else {
							o[prop] = obj[prop]
							o[prop + "_fmt"] = null;
						}
					} else {
						o[prop] = mungeTimes(obj[prop])
					}
				}
			}
			return o;
		}
	} else {
		return obj;
	}
}

$(document).ready(function () {
	var template = null;
	var vars = null;

	var render = function () {
		$('#content').html(Mustache.to_html(template, vars));
	};

	var updateVars = function () {
		$.get('/json', function (data) {
			lastRefreshed = new Date();
			vars = mungeTimes(data);
			if (template) {
				render();
			}
		});
	};

	/* update template */
	$.get('/template', function (data) {
		template = data;
		if (vars) {
			render();
		}
	});

	updateVars();
	setInterval(updateVars, 1000);
});
